"""Outlook: mail and calendar of the signed-in Microsoft 365 account, through
Microsoft Graph.

ai-os starts it in two ways:

- `server.py`: an MCP session over stdio, for a turn. AIOS_ACCESS_TOKEN is the
  access token of the turn's sign-in (the connector's `oauth:` client; with "each
  user's own account", the user's own). The server keeps nothing: no token, no mail.
- `server.py preflight send_mail <arguments>`: the question put to the human before
  a mail is sent, or why it is refused outright.

Times an agent gives without an offset are in AIOS_TIMEZONE, and Graph is asked to
answer in it too, so what the agent reads back is local time as well.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
from mcp.server.mcpserver import MCPServer

DEFAULTS = {"read_only": False, "max_results": 25, "max_body_chars": 20000}
SETTINGS = DEFAULTS | json.loads(os.environ.get("AIOS_CONNECTOR_SETTINGS") or "{}")
TIMEZONE = os.environ.get("AIOS_TIMEZONE") or "UTC"
# Only the tests set this: ai-os passes a code connector nothing it does not declare.
GRAPH = os.environ.get("OUTLOOK_GRAPH_URL") or "https://graph.microsoft.com/v1.0"
TIMEOUT = aiohttp.ClientTimeout(total=60)

MESSAGE_FIELDS = "id,receivedDateTime,from,subject,bodyPreview,isRead,hasAttachments,webLink"
EVENT_FIELDS = (
    "id,subject,start,end,isAllDay,location,organizer,attendees,isOnlineMeeting,"
    "onlineMeeting,showAs,responseStatus,isOrganizer,isCancelled,webLink"
)


class GraphError(Exception):
    """What Graph refused, in words an agent can pass on."""


class Graph:
    """Calls Microsoft Graph as the signed-in user."""

    def __init__(self, token: str | None, base: str = GRAPH, timezone: str = TIMEZONE) -> None:
        self._token = token
        self._base = base.rstrip("/")
        self.timezone = timezone

    async def call(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        text: bool = False,
    ) -> dict[str, Any]:
        if not self._token:
            raise GraphError("not signed in to Microsoft: sign in on the Connectors page")
        prefer = f'outlook.timezone="{self.timezone}"'
        if text:
            prefer += ', outlook.body-content-type="text"'
        headers = {"Authorization": f"Bearer {self._token}", "Prefer": prefer}
        async with (
            aiohttp.ClientSession(timeout=TIMEOUT) as session,
            session.request(
                method, self._base + path, params=params, json=body, headers=headers
            ) as response,
        ):
            raw = await response.read()
            data = json.loads(raw) if raw.strip() else {}
            if response.status == 401:
                raise GraphError("Microsoft refused the sign-in (401): sign in again")
            if response.status >= 400:
                error = data.get("error", {}) if isinstance(data, dict) else {}
                message = error.get("message") or raw.decode(errors="replace")[:300]
                raise GraphError(f"Graph answered {response.status}: {message}")
            return data if isinstance(data, dict) else {}


def _id(value: str) -> str:
    return quote(value, safe="")


def _local(value: str, zone: str) -> datetime:
    """A time as given, in `zone` without an offset; one with an offset is moved there."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    return moment


def _utc(value: str, zone: str) -> str:
    moment = _local(value, zone).replace(tzinfo=ZoneInfo(zone)).astimezone(ZoneInfo("UTC"))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: str, zone: str) -> dict[str, str]:
    return {"dateTime": _local(value, zone).isoformat(timespec="seconds"), "timeZone": zone}


def _recipients(addresses: list[str] | None) -> list[dict]:
    return [{"emailAddress": {"address": a.strip()}} for a in addresses or [] if a.strip()]


def _who(recipient: dict | None) -> str:
    address = (recipient or {}).get("emailAddress") or {}
    name, mail = address.get("name") or "", address.get("address") or ""
    return f"{name} <{mail}>" if name and mail and name != mail else mail or name


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[cut off at {limit} characters]"


def _time(value: dict | None) -> str:
    return ((value or {}).get("dateTime") or "")[:16]


def _limit(limit: int | None) -> int:
    most = int(SETTINGS["max_results"])
    return max(1, min(limit or most, most))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _message_row(m: dict, zone: str) -> dict:
    return {
        "id": m.get("id"),
        "received": _local(m["receivedDateTime"].replace("Z", "+00:00"), zone).isoformat()[:16]
        if m.get("receivedDateTime")
        else None,
        "from": _who(m.get("from")),
        "subject": m.get("subject"),
        "preview": m.get("bodyPreview"),
        "read": m.get("isRead"),
        "attachments": m.get("hasAttachments"),
        "link": m.get("webLink"),
    }


def _event_row(e: dict) -> dict:
    return {
        "id": e.get("id"),
        "subject": e.get("subject"),
        "start": _time(e.get("start")),
        "end": _time(e.get("end")),
        "all_day": e.get("isAllDay"),
        "location": (e.get("location") or {}).get("displayName") or None,
        "organizer": _who(e.get("organizer")),
        "attendees": [
            {"who": _who(a), "response": (a.get("status") or {}).get("response")}
            for a in e.get("attendees") or []
        ],
        "my_response": (e.get("responseStatus") or {}).get("response"),
        "i_organize": e.get("isOrganizer"),
        "show_as": e.get("showAs"),
        "cancelled": e.get("isCancelled"),
        "online_meeting": ((e.get("onlineMeeting") or {}).get("joinUrl")),
        "link": e.get("webLink"),
    }


class Outlook:
    """The tools. Each reads or writes the signed-in user's own mailbox and calendar."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph
        self._zone = graph.timezone

    # Mail, reading

    async def whoami(self) -> str:
        """The signed-in account: name and mail address, and the time zone used."""
        me = await self._graph.call("GET", "/me", {"$select": "displayName,mail,userPrincipalName"})
        mail = me.get("mail") or me.get("userPrincipalName")
        return _json({"name": me.get("displayName"), "mail": mail, "timezone": self._zone})

    async def list_folders(self) -> str:
        """The mail folders with their ids and unread counts."""
        data = await self._graph.call(
            "GET",
            "/me/mailFolders",
            {"$top": "100", "$select": "id,displayName,unreadItemCount,totalItemCount"},
        )
        return _json(
            [
                {
                    "id": f["id"],
                    "name": f.get("displayName"),
                    "unread": f.get("unreadItemCount"),
                    "total": f.get("totalItemCount"),
                }
                for f in data.get("value", [])
            ]
        )

    async def list_messages(
        self,
        folder: str = "inbox",
        search: str | None = None,
        unread_only: bool = False,
        since: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Mails in a folder, newest first. `folder`: inbox, sentitems, drafts,
        archive, deleteditems, junkemail, or an id from list_folders. `search`: words
        as in Outlook's search box (from:, subject:, ...); then `unread_only` and
        `since` (local date or time) still apply, but on the results found."""
        params = {"$top": str(_limit(limit)), "$select": MESSAGE_FIELDS}
        if search:
            params["$search"] = '"' + search.replace('"', "") + '"'
        else:
            first = _utc(since, self._zone) if since else "1900-01-01T00:00:00Z"
            filters = [f"receivedDateTime ge {first}"]
            if unread_only:
                filters.append("isRead eq false")
            params["$filter"] = " and ".join(filters)
            params["$orderby"] = "receivedDateTime desc"
        data = await self._graph.call("GET", f"/me/mailFolders/{_id(folder)}/messages", params)
        rows = [_message_row(m, self._zone) for m in data.get("value", [])]
        if search and unread_only:
            rows = [r for r in rows if not r["read"]]
        if search and since:
            first = _local(since, self._zone).isoformat()[:16]
            rows = [r for r in rows if (r["received"] or "") >= first]
        return _json(rows)

    async def get_message(self, message_id: str) -> str:
        """One mail in full: sender, recipients, text and the names of its attachments."""
        m = await self._graph.call(
            "GET",
            f"/me/messages/{_id(message_id)}",
            {
                "$select": MESSAGE_FIELDS + ",toRecipients,ccRecipients,replyTo,body",
                "$expand": "attachments($select=name,contentType,size)",
            },
            text=True,
        )
        text = _cut((m.get("body") or {}).get("content") or "", SETTINGS["max_body_chars"])
        return _json(
            {
                **_message_row(m, self._zone),
                "to": [_who(r) for r in m.get("toRecipients") or []],
                "cc": [_who(r) for r in m.get("ccRecipients") or []],
                "reply_to": [_who(r) for r in m.get("replyTo") or []],
                "body": text,
                "attachment_names": [
                    {"name": a.get("name"), "type": a.get("contentType"), "size": a.get("size")}
                    for a in m.get("attachments") or []
                ],
            }
        )

    # Mail, writing

    async def create_draft(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> str:
        """A new mail saved in Drafts, not sent. Returns its id and a link to open it."""
        draft = await self._graph.call(
            "POST",
            "/me/messages",
            body={
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": _recipients(to),
                "ccRecipients": _recipients(cc),
            },
        )
        return _json({"id": draft.get("id"), "link": draft.get("webLink")})

    async def create_reply_draft(self, message_id: str, body: str, reply_all: bool = False) -> str:
        """A reply to a mail, saved in Drafts with the quoted original, not sent."""
        action = "createReplyAll" if reply_all else "createReply"
        draft = await self._graph.call(
            "POST", f"/me/messages/{_id(message_id)}/{action}", body={"comment": body}
        )
        return _json(
            {
                "id": draft.get("id"),
                "to": [_who(r) for r in draft.get("toRecipients") or []],
                "link": draft.get("webLink"),
            }
        )

    async def send_draft(self, message_id: str) -> str:
        """Sends a mail from Drafts. A human approves it first."""
        await self._graph.call("POST", f"/me/messages/{_id(message_id)}/send")
        return "sent"

    async def send_mail(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> str:
        """Sends a new mail at once, kept in Sent Items. A human approves it first."""
        await self._graph.call(
            "POST",
            "/me/sendMail",
            body={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": _recipients(to),
                    "ccRecipients": _recipients(cc),
                },
                "saveToSentItems": True,
            },
        )
        return "sent"

    async def move_message(self, message_id: str, folder: str) -> str:
        """Moves a mail to a folder (archive, deleteditems, or an id from
        list_folders). Its id changes: returns the new one."""
        moved = await self._graph.call(
            "POST", f"/me/messages/{_id(message_id)}/move", body={"destinationId": folder}
        )
        return _json({"id": moved.get("id")})

    async def mark_read(self, message_id: str, read: bool = True) -> str:
        """Marks a mail read, or unread with `read` false."""
        await self._graph.call("PATCH", f"/me/messages/{_id(message_id)}", body={"isRead": read})
        return "read" if read else "unread"

    # Calendar, reading

    async def list_events(self, start: str, end: str, limit: int | None = None) -> str:
        """Events between two local times (ISO, e.g. 2026-11-27T00:00), recurring ones
        as single occurrences, earliest first."""
        data = await self._graph.call(
            "GET",
            "/me/calendarView",
            {
                "startDateTime": _utc(start, self._zone),
                "endDateTime": _utc(end, self._zone),
                "$orderby": "start/dateTime",
                "$top": str(_limit(limit)),
                "$select": EVENT_FIELDS,
            },
        )
        return _json([_event_row(e) for e in data.get("value", [])])

    async def get_event(self, event_id: str) -> str:
        """One event in full, with its text."""
        e = await self._graph.call(
            "GET", f"/me/events/{_id(event_id)}", {"$select": EVENT_FIELDS + ",body"}, text=True
        )
        text = _cut((e.get("body") or {}).get("content") or "", SETTINGS["max_body_chars"])
        return _json({**_event_row(e), "body": text})

    async def free_busy(self, addresses: list[str], start: str, end: str) -> str:
        """When people (mail addresses, the user's own too) are busy between two local
        times: their busy blocks, as far as their calendars let the user see."""
        data = await self._graph.call(
            "POST",
            "/me/calendar/getSchedule",
            body={
                "schedules": addresses,
                "startTime": _when(start, self._zone),
                "endTime": _when(end, self._zone),
                "availabilityViewInterval": 30,
            },
        )
        return _json(
            [
                {
                    "who": s.get("scheduleId"),
                    "error": (s.get("error") or {}).get("message"),
                    "busy": [
                        {
                            "status": i.get("status"),
                            "start": _time(i.get("start")),
                            "end": _time(i.get("end")),
                        }
                        for i in s.get("scheduleItems") or []
                    ],
                }
                for s in data.get("value", [])
            ]
        )

    async def find_people(self, query: str) -> str:
        """People the user works with, by name: their mail addresses, title, department."""
        data = await self._graph.call(
            "GET",
            "/me/people",
            {
                "$search": '"' + query.replace('"', "") + '"',
                "$top": "10",
                "$select": "displayName,scoredEmailAddresses,jobTitle,department",
            },
        )
        return _json(
            [
                {
                    "name": p.get("displayName"),
                    "mail": [a.get("address") for a in p.get("scoredEmailAddresses") or []],
                    "title": p.get("jobTitle"),
                    "department": p.get("department"),
                }
                for p in data.get("value", [])
            ]
        )

    # Calendar, writing

    async def create_event(
        self,
        subject: str,
        start: str,
        end: str,
        attendees: list[str] | None = None,
        location: str | None = None,
        body: str | None = None,
        all_day: bool = False,
        online_meeting: bool = False,
    ) -> str:
        """A new event; with attendees, Outlook sends them the invitation. Times are
        local ISO; for `all_day`, dates (the end day included). `online_meeting`: with
        a Teams link. A human approves it first."""
        event: dict[str, Any] = {"subject": subject, "attendees": _attendees(attendees)}
        if all_day:
            first = date.fromisoformat(start[:10])
            last = date.fromisoformat(end[:10]) if end else first
            event |= {
                "isAllDay": True,
                "start": {"dateTime": f"{first}T00:00:00", "timeZone": self._zone},
                "end": {
                    "dateTime": f"{max(last, first) + timedelta(days=1)}T00:00:00",
                    "timeZone": self._zone,
                },
            }
        else:
            event |= {"start": _when(start, self._zone), "end": _when(end, self._zone)}
        if location:
            event["location"] = {"displayName": location}
        if body:
            event["body"] = {"contentType": "Text", "content": body}
        if online_meeting:
            event |= {"isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness"}
        created = await self._graph.call("POST", "/me/events", body=event)
        return _json(_event_row(created))

    async def update_event(
        self,
        event_id: str,
        subject: str | None = None,
        start: str | None = None,
        end: str | None = None,
        location: str | None = None,
        body: str | None = None,
        attendees: list[str] | None = None,
    ) -> str:
        """Changes an event the user organizes; only what is given changes.
        `attendees` replaces the whole list. Attendees get the update. A human
        approves it first."""
        change: dict[str, Any] = {}
        if subject is not None:
            change["subject"] = subject
        if start is not None:
            change["start"] = _when(start, self._zone)
        if end is not None:
            change["end"] = _when(end, self._zone)
        if location is not None:
            change["location"] = {"displayName": location}
        if body is not None:
            change["body"] = {"contentType": "Text", "content": body}
        if attendees is not None:
            change["attendees"] = _attendees(attendees)
        if not change:
            raise GraphError("nothing to change")
        updated = await self._graph.call("PATCH", f"/me/events/{_id(event_id)}", body=change)
        return _json(_event_row(updated))

    async def delete_event(self, event_id: str, comment: str | None = None) -> str:
        """Removes an event. One the user organizes with attendees is cancelled, and
        they are told (with `comment`); to turn down someone else's invitation, use
        respond_to_event instead. A human approves it first."""
        path = f"/me/events/{_id(event_id)}"
        event = await self._graph.call("GET", path, {"$select": "isOrganizer,attendees"})
        if event.get("isOrganizer") and event.get("attendees"):
            await self._graph.call("POST", f"{path}/cancel", body={"comment": comment or ""})
            return "cancelled, attendees told"
        await self._graph.call("DELETE", path)
        return "deleted"

    async def respond_to_event(
        self,
        event_id: str,
        response: Literal["accept", "tentative", "decline"],
        comment: str | None = None,
        send_response: bool = True,
    ) -> str:
        """Accepts, tentatively accepts or declines an invitation, telling the
        organizer unless `send_response` is false. A human approves it first."""
        action = {"accept": "accept", "tentative": "tentativelyAccept", "decline": "decline"}
        await self._graph.call(
            "POST",
            f"/me/events/{_id(event_id)}/{action[response]}",
            body={"comment": comment or "", "sendResponse": send_response},
        )
        return response


def _attendees(addresses: list[str] | None) -> list[dict]:
    return [{**r, "type": "required"} for r in _recipients(addresses)]


READING = [
    "whoami",
    "list_folders",
    "list_messages",
    "get_message",
    "list_events",
    "get_event",
    "free_busy",
    "find_people",
]
WRITING = [
    "create_draft",
    "create_reply_draft",
    "send_draft",
    "send_mail",
    "move_message",
    "mark_read",
    "create_event",
    "update_event",
    "delete_event",
    "respond_to_event",
]


def preflight(tool: str, arguments: dict) -> dict:
    """Asked before a human is: refuse a mail that cannot go out, word the rest."""
    if tool != "send_mail":
        return {"ask": f"Run {tool}?"}
    to = [a for a in arguments.get("to") or [] if str(a).strip()]
    cc = [a for a in arguments.get("cc") or [] if str(a).strip()]
    wrong = [a for a in [*to, *cc] if "@" not in str(a)]
    if not to:
        return {"deny": "the mail has no recipient"}
    if wrong:
        return {"deny": f"not a mail address: {', '.join(map(str, wrong))}"}
    copy = f", copy to {', '.join(cc)}" if cc else ""
    return {"ask": f"Send the mail {arguments.get('subject', '')!r} to {', '.join(to)}{copy}?"}


def server(outlook: Outlook) -> MCPServer:
    mcp = MCPServer("outlook")
    for name in READING + ([] if SETTINGS["read_only"] else WRITING):
        mcp.tool()(getattr(outlook, name))
    return mcp


if __name__ == "__main__":
    if sys.argv[1:2] == ["preflight"]:
        print(json.dumps(preflight(sys.argv[2], json.loads(sys.argv[3]))))
    else:
        server(Outlook(Graph(os.environ.get("AIOS_ACCESS_TOKEN")))).run()
