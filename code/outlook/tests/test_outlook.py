"""The connector's own tests, against a fake Graph. Run them with ai-os's Python, in
the ai-os clone: `uv run pytest <config>/code/outlook/tests`."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = Path(__file__).parents[1] / "server.py"
sys.path.insert(0, str(SERVER.parent))

import server as outlook  # noqa: E402


class FakeGraph:
    """Records each request and answers from `answers`, keyed by method and path."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.answers: dict[str, tuple[int, dict]] = {}
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        self.server = TestServer(app)

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        key = f"{request.method} {request.rel_url.raw_path}"
        self.requests.append(
            {
                "key": key,
                "query": dict(request.query),
                "body": body,
                "auth": request.headers.get("Authorization"),
                "prefer": request.headers.get("Prefer"),
            }
        )
        status, answer = self.answers.get(key, (200, {}))
        return web.json_response(answer, status=status)


@pytest.fixture
async def graph():  # type: ignore[no-untyped-def]
    fake = FakeGraph()
    await fake.server.start_server()
    yield fake
    await fake.server.close()


def _outlook(fake: FakeGraph, token: str | None = "tok") -> outlook.Outlook:
    return outlook.Outlook(
        outlook.Graph(token, str(fake.server.make_url("/")), timezone="Europe/Zurich")
    )


async def test_every_call_carries_the_token_and_asks_for_local_time(graph) -> None:
    graph.answers["GET /me"] = (200, {"displayName": "Ada", "mail": "ada@example.com"})
    me = json.loads(await _outlook(graph).whoami())
    assert me["mail"] == "ada@example.com"
    assert graph.requests[0]["auth"] == "Bearer tok"
    assert graph.requests[0]["prefer"] == 'outlook.timezone="Europe/Zurich"'


async def test_without_a_sign_in_nothing_is_called(graph) -> None:
    with pytest.raises(outlook.GraphError, match="not signed in"):
        await _outlook(graph, token=None).whoami()
    assert graph.requests == []


async def test_a_refused_token_says_to_sign_in_again(graph) -> None:
    graph.answers["GET /me"] = (401, {"error": {"message": "expired"}})
    with pytest.raises(outlook.GraphError, match="sign in again"):
        await _outlook(graph).whoami()


async def test_graphs_own_error_is_passed_on(graph) -> None:
    graph.answers["GET /me/messages/x"] = (404, {"error": {"message": "not found"}})
    with pytest.raises(outlook.GraphError, match="404: not found"):
        await _outlook(graph).get_message("x")


async def test_unread_mail_is_filtered_newest_first_and_received_in_local_time(graph) -> None:
    graph.answers["GET /me/mailFolders/inbox/messages"] = (
        200,
        {
            "value": [
                {
                    "id": "m1",
                    "receivedDateTime": "2026-11-26T08:30:00Z",
                    "from": {"emailAddress": {"name": "Bob", "address": "bob@example.com"}},
                    "subject": "Hi",
                    "isRead": False,
                }
            ]
        },
    )
    rows = json.loads(await _outlook(graph).list_messages(unread_only=True, since="2026-11-26"))
    query = graph.requests[0]["query"]
    assert query["$filter"] == "receivedDateTime ge 2026-11-25T23:00:00Z and isRead eq false"
    assert query["$orderby"] == "receivedDateTime desc"
    assert rows[0]["from"] == "Bob <bob@example.com>"
    assert rows[0]["received"] == "2026-11-26T09:30"


async def test_a_search_is_quoted_and_not_combined_with_a_filter(graph) -> None:
    await _outlook(graph).list_messages(search='from:bob "budget"')
    query = graph.requests[0]["query"]
    assert query["$search"] == '"from:bob budget"'
    assert "$filter" not in query


async def test_a_long_body_is_cut_off(graph, monkeypatch) -> None:
    monkeypatch.setitem(outlook.SETTINGS, "max_body_chars", 10)
    graph.answers["GET /me/messages/m1"] = (200, {"id": "m1", "body": {"content": "x" * 50}})
    mail = json.loads(await _outlook(graph).get_message("m1"))
    assert mail["body"].startswith("x" * 10 + "\n[cut off at 10")
    assert 'outlook.body-content-type="text"' in graph.requests[0]["prefer"]


async def test_an_id_is_one_path_segment(graph) -> None:
    await _outlook(graph).mark_read("a/b=")
    assert graph.requests[0]["key"] == "PATCH /me/messages/a%2Fb="


async def test_events_are_asked_for_in_utc_and_shown_local(graph) -> None:
    graph.answers["GET /me/calendarView"] = (
        200,
        {
            "value": [
                {
                    "id": "e1",
                    "subject": "Standup",
                    "start": {"dateTime": "2026-11-27T09:00:00.0000000"},
                    "end": {"dateTime": "2026-11-27T09:15:00.0000000"},
                }
            ]
        },
    )
    rows = json.loads(
        await _outlook(graph).list_events("2026-11-27T00:00", "2026-11-28T00:00", limit=500)
    )
    query = graph.requests[0]["query"]
    assert query["startDateTime"] == "2026-11-26T23:00:00Z"
    assert query["$top"] == "25"  # the limit is the setting's
    assert rows[0]["start"] == "2026-11-27T09:00" and rows[0]["end"] == "2026-11-27T09:15"


async def test_an_event_is_created_in_local_time_with_a_teams_link(graph) -> None:
    await _outlook(graph).create_event(
        "Review", "2026-11-27T14:00", "2026-11-27T15:00", ["bob@example.com"], online_meeting=True
    )
    body = graph.requests[0]["body"]
    assert body["start"] == {"dateTime": "2026-11-27T14:00:00", "timeZone": "Europe/Zurich"}
    bob = {"emailAddress": {"address": "bob@example.com"}, "type": "required"}
    assert body["attendees"] == [bob]
    assert body["onlineMeetingProvider"] == "teamsForBusiness"


async def test_an_all_day_event_ends_the_day_after_its_last_day(graph) -> None:
    await _outlook(graph).create_event("Trip", "2026-12-01", "2026-12-03", all_day=True)
    body = graph.requests[0]["body"]
    assert body["start"]["dateTime"] == "2026-12-01T00:00:00"
    assert body["end"]["dateTime"] == "2026-12-04T00:00:00"


async def test_deleting_a_meeting_one_organizes_cancels_it_for_the_attendees(graph) -> None:
    graph.answers["GET /me/events/e1"] = (200, {"isOrganizer": True, "attendees": [{}]})
    assert await _outlook(graph).delete_event("e1", "sorry") == "cancelled, attendees told"
    assert graph.requests[1]["key"] == "POST /me/events/e1/cancel"
    graph.answers["GET /me/events/e2"] = (200, {"isOrganizer": True, "attendees": []})
    assert await _outlook(graph).delete_event("e2") == "deleted"
    assert graph.requests[3]["key"] == "DELETE /me/events/e2"


def _preflight(arguments: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(SERVER), "preflight", "send_mail", json.dumps(arguments)],
        capture_output=True,
        text=True,
        env={"AIOS_CONNECTOR_SETTINGS": "{}"},
        check=True,
    )
    return json.loads(result.stdout)


def test_the_human_is_asked_who_gets_the_mail() -> None:
    answer = _preflight({"to": ["bob@example.com"], "cc": ["eve@example.com"], "subject": "Hi"})
    assert answer == {"ask": "Send the mail 'Hi' to bob@example.com, copy to eve@example.com?"}


def test_a_mail_without_a_proper_recipient_is_refused_without_asking() -> None:
    assert _preflight({"to": [], "subject": "Hi"}) == {"deny": "the mail has no recipient"}
    assert "not a mail address: bob" in _preflight({"to": ["bob"], "subject": "Hi"})["deny"]


async def _tools(settings: dict) -> list[str]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env={"AIOS_CONNECTOR_SETTINGS": json.dumps(settings), "PATH": os.environ["PATH"]},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return sorted(t.name for t in (await session.list_tools()).tools)


async def test_read_only_offers_only_the_tools_that_read() -> None:
    assert await _tools({"read_only": True}) == sorted(outlook.READING)
    assert await _tools({}) == sorted(outlook.READING + outlook.WRITING)
