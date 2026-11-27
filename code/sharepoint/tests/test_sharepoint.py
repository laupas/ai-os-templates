"""The connector's own tests, against a fake Graph. Run them with ai-os's Python, in
the ai-os clone: `uv run pytest <config>/code/sharepoint/tests`."""

import base64
import io
import json
import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = Path(__file__).parents[1] / "server.py"
sys.path.insert(0, str(SERVER.parent))

import server as sharepoint  # noqa: E402

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


DOCX = _zip(
    {
        "word/document.xml": f"<w:document {W}><w:body>"
        "<w:p><w:r><w:t>Project</w:t></w:r><w:r><w:tab/><w:t>Alpha</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Second line</w:t></w:r></w:p>"
        "</w:body></w:document>"
    }
)
PPTX = _zip(
    {
        "ppt/slides/slide10.xml": f"<p {A}><a:p><a:t>Ten</a:t></a:p></p>",
        "ppt/slides/slide2.xml": f"<p {A}><a:p><a:t>Two</a:t></a:p></p>",
    }
)
XLSX = _zip(
    {
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Budget" r:id="rId1"/>'
        "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": f'<Relationships {REL}><Relationship Id="rId1" '
        'Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f"<sst {S}><si><t>Item</t></si><si><t>Cost</t></si></sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row><c t="s"><v>0</v></c>'
        '<c t="s"><v>1</v></c></row><row><c t="inlineStr"><is><t>Rent</t></is></c>'
        "<c><v>1200</v></c></row></sheetData></worksheet>",
    }
)


def _pdf(*pages: str) -> bytes:
    """A small PDF with one line of text per page."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = "%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode()


PDF = _pdf("Quality policy", "Second page")


def test_a_pdf_is_read_page_by_page() -> None:
    text, notes, pictures = sharepoint.read_pdf("x.pdf", PDF)
    assert text == "--- Page 1\nQuality policy\n\n--- Page 2\nSecond page"
    assert notes == {} and pictures == []


def test_a_broken_pdf_is_an_error_not_a_crash() -> None:
    with pytest.raises(sharepoint.GraphError, match="could not be read"):
        sharepoint.read_pdf("x.pdf", b"%PDF-1.4 nothing")


def _png_size(picture: bytes) -> tuple[int, int, int]:
    """Width, height and colour type of a PNG, after checking it decodes."""
    assert picture.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, colour = struct.unpack(">IIBB", picture[16:26])
    idat = picture.index(b"IDAT")
    (length,) = struct.unpack(">I", picture[idat - 4 : idat])
    assert len(zlib.decompress(picture[idat + 4 : idat + 4 + length])) == (width + 1) * height
    return width, height, colour


def test_a_scanned_page_becomes_a_grayscale_picture_of_the_page() -> None:
    text, notes, pictures = sharepoint.read_pdf("x.pdf", _pdf("Cover", "", "End"))
    assert text == (
        "--- Page 1\nCover\n\n--- Page 2\n[scanned: its picture follows]\n\n--- Page 3\nEnd"
    )
    assert notes == {"pictures_of_pages": [2]}
    (picture,) = pictures
    width, height, colour = _png_size(picture)
    assert height == sharepoint.PAGE_PIXELS and width == round(612 * 1568 / 792)
    assert colour == 0


def test_scanned_pages_beyond_the_setting_are_named_not_shown(monkeypatch) -> None:
    monkeypatch.setitem(sharepoint.SETTINGS, "max_scanned_pages", 1)
    _, notes, pictures = sharepoint.read_pdf("x.pdf", _pdf("", "", ""))
    assert len(pictures) == 1
    assert notes["pictures_of_pages"] == [1]
    assert notes["scanned_pages_not_shown"] == [2, 3]
    assert "first 1" in notes["why"]


def test_without_pypdfium2_scanned_pages_are_named(monkeypatch) -> None:
    monkeypatch.setattr(sharepoint, "pypdfium2", None)
    _, notes, pictures = sharepoint.read_pdf("x.pdf", _pdf("Cover", ""))
    assert pictures == []
    assert notes == {"scanned_pages_not_shown": [2], "why": notes["why"]}
    assert "update it" in notes["why"]


def test_word_is_read_paragraph_by_paragraph() -> None:
    assert sharepoint.docx_text(DOCX) == "Project\tAlpha\nSecond line"


def test_powerpoint_is_read_slide_by_slide_in_order() -> None:
    assert sharepoint.pptx_text(PPTX) == "--- Slide 1\nTwo\n\n--- Slide 2\nTen"


def test_excel_is_read_sheet_by_sheet() -> None:
    assert sharepoint.xlsx_text(XLSX) == "--- Sheet Budget\nItem\tCost\nRent\t1200"


def test_a_broken_office_file_is_an_error_not_a_crash() -> None:
    with pytest.raises(sharepoint.GraphError, match="could not be read"):
        sharepoint.text_of("x.docx", b"not a zip")


class FakeGraph:
    """Records each request and answers from `answers`, keyed by method and raw path.
    A file's content is a redirect to `/download` on another server, as Graph does."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.answers: dict[str, tuple[int, dict]] = {}
        self.file = b""
        self.download_auth: str | None = "unset"
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        self.server = TestServer(app)
        files = web.Application()
        files.router.add_get("/download", self._download)
        self.files = TestServer(files)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        key = f"{request.method} {request.rel_url.raw_path}"
        body = json.loads(raw) if raw and request.content_type == "application/json" else raw
        self.requests.append(
            {
                "key": key,
                "query": dict(request.query),
                "body": body,
                "auth": request.headers.get("Authorization"),
            }
        )
        if key.endswith("/content") and request.method == "GET":
            raise web.HTTPFound(str(self.files.make_url("/download")))
        status, answer = self.answers.get(key, (200, {}))
        return web.json_response(answer, status=status)

    async def _download(self, request: web.Request) -> web.Response:
        self.download_auth = request.headers.get("Authorization")
        return web.Response(body=self.file)


@pytest.fixture
async def graph():  # type: ignore[no-untyped-def]
    fake = FakeGraph()
    await fake.server.start_server()
    await fake.files.start_server()
    yield fake
    await fake.server.close()
    await fake.files.close()


def _sp(fake: FakeGraph, token: str | None = "tok") -> sharepoint.SharePoint:
    return sharepoint.SharePoint(sharepoint.Graph(token, str(fake.server.make_url("/"))))


async def test_without_a_sign_in_nothing_is_called(graph) -> None:
    with pytest.raises(sharepoint.GraphError, match="not signed in"):
        await _sp(graph, token=None).list_folder()
    assert graph.requests == []


async def test_search_finds_files_with_their_summary(graph) -> None:
    graph.answers["POST /search/query"] = (
        200,
        {
            "value": [
                {
                    "hitsContainers": [
                        {
                            "hits": [
                                {
                                    "summary": "the <c0>budget</c0> for 2027<ddd/>",
                                    "resource": {
                                        "id": "i1",
                                        "name": "Budget.xlsx",
                                        "file": {},
                                        "parentReference": {"driveId": "d1"},
                                    },
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    )
    rows = json.loads(await _sp(graph).search("budget filetype:xlsx", limit=500))
    request = graph.requests[0]["body"]["requests"][0]
    assert request["entityTypes"] == ["driveItem"] and request["size"] == 25
    assert rows[0]["item_id"] == "i1" and rows[0]["drive_id"] == "d1"
    assert rows[0]["summary"] == "the budget for 2027"


async def test_a_site_is_found_by_its_address(graph) -> None:
    await _sp(graph).get_site("https://contoso.sharepoint.com/sites/Team Room")
    assert graph.requests[0]["key"] == "GET /sites/contoso.sharepoint.com:/sites/Team%20Room"


async def test_a_folder_is_listed_by_path_and_onedrive_without_a_drive(graph) -> None:
    await _sp(graph).list_folder("d1", "/Projects/2026/")
    await _sp(graph).list_folder()
    assert graph.requests[0]["key"] == "GET /drives/d1/root:/Projects/2026:/children"
    assert graph.requests[1]["key"] == "GET /me/drive/root/children"


async def _read(fake: FakeGraph, item: str) -> dict:
    (answer,) = await _sp(fake).read_file("d1", item)
    return json.loads(answer)


async def test_a_word_file_is_downloaded_without_the_token_and_read(graph) -> None:
    graph.answers["GET /drives/d1/items/i1"] = (200, {"id": "i1", "name": "Plan.docx", "size": 9})
    graph.file = DOCX
    answer = await _read(graph, "i1")
    assert answer["text"] == "Project\tAlpha\nSecond line"
    assert graph.requests[1]["auth"] == "Bearer tok"
    assert graph.download_auth is None  # the pre-signed address gets no token


async def test_a_pdf_is_read(graph) -> None:
    graph.answers["GET /drives/d1/items/p"] = (200, {"id": "p", "name": "Policy.PDF", "size": 9})
    graph.file = PDF
    assert (await _read(graph, "p"))["text"].startswith("--- Page 1\nQuality policy")
    assert graph.requests[1]["query"] == {}


async def test_a_scanned_pdf_comes_as_pictures_of_its_pages(graph) -> None:
    graph.answers["GET /drives/d1/items/s"] = (200, {"name": "Scan.pdf", "size": 9})
    graph.file = _pdf("", "")
    row, *pages = await _sp(graph).read_file("d1", "s")
    assert json.loads(row)["pictures_of_pages"] == [1, 2]
    assert [p.to_image_content().mime_type for p in pages] == ["image/png", "image/png"]
    _png_size(base64.b64decode(pages[0].to_image_content().data))


async def test_a_scanned_pdf_says_so_when_it_shows_no_page(graph, monkeypatch) -> None:
    monkeypatch.setitem(sharepoint.SETTINGS, "max_scanned_pages", 0)
    graph.answers["GET /drives/d1/items/s"] = (200, {"name": "Scan.pdf", "size": 9})
    graph.file = _pdf("")
    answer = await _read(graph, "s")
    assert answer["text"] is None and answer["scanned_pages_not_shown"] == [1]


async def test_an_old_word_file_is_turned_into_a_pdf_by_microsoft(graph) -> None:
    graph.answers["GET /drives/d1/items/o"] = (200, {"name": "Old SOP.doc", "size": 9})
    graph.file = PDF
    assert "Quality policy" in (await _read(graph, "o"))["text"]
    assert graph.requests[1]["key"] == "GET /drives/d1/items/o/content"
    assert graph.requests[1]["query"] == {"format": "pdf"}


async def test_a_picture_comes_as_itself(graph) -> None:
    graph.answers["GET /drives/d1/items/g"] = (200, {"name": "Chart.JPG", "size": 9})
    graph.file = b"\xff\xd8\xff picture"
    row, picture = await _sp(graph).read_file("d1", "g")
    assert json.loads(row)["name"] == "Chart.JPG"
    content = picture.to_image_content()
    assert content.mime_type == "image/jpeg"
    assert base64.b64decode(content.data) == graph.file


async def test_an_unknown_kind_and_a_big_file_are_not_downloaded(graph, monkeypatch) -> None:
    graph.answers["GET /drives/d1/items/z"] = (200, {"id": "z", "name": "Model.dwg", "size": 9})
    answer = await _read(graph, "z")
    assert answer["text"] is None and "not read" in answer["why"]
    monkeypatch.setitem(sharepoint.SETTINGS, "max_file_mb", 1)
    graph.answers["GET /drives/d1/items/b"] = (200, {"name": "Big.docx", "size": 2_000_000})
    assert "larger than 1 MB" in (await _read(graph, "b"))["why"]
    monkeypatch.setitem(sharepoint.SETTINGS, "max_file_mb", 20)
    graph.answers["GET /drives/d1/items/i"] = (200, {"name": "Photo.png", "size": 6_000_000})
    assert "larger than 5 MB" in (await _read(graph, "i"))["why"]
    assert all(not r["key"].endswith("/content") for r in graph.requests)


async def test_a_long_text_is_cut_off(graph, monkeypatch) -> None:
    monkeypatch.setitem(sharepoint.SETTINGS, "max_text_chars", 5)
    graph.answers["GET /drives/d1/items/t"] = (200, {"name": "notes.txt", "size": 9})
    graph.file = b"abcdefghij"
    text = (await _read(graph, "t"))["text"]
    assert text.startswith("abcde\n[cut off at 5")


async def test_an_upload_does_not_replace_unless_asked(graph) -> None:
    await _sp(graph).upload_text_file("d1", "Notes", "a.md", "# Hi")
    request = graph.requests[0]
    assert request["key"] == "PUT /drives/d1/root:/Notes/a.md:/content"
    assert request["query"] == {"@microsoft.graph.conflictBehavior": "fail"}
    assert request["body"] == b"# Hi"


async def test_a_move_finds_the_folder_by_path_and_never_replaces(graph) -> None:
    graph.answers["GET /drives/d1/root:/Archive/2025"] = (200, {"id": "f9", "folder": {}})
    graph.answers["PATCH /drives/d1/items/i1"] = (200, {"id": "i1", "name": "a.docx"})
    row = json.loads(await _sp(graph).move_item("d1", "i1", "/Archive/2025/"))
    move = graph.requests[1]
    assert move["body"] == {"parentReference": {"id": "f9"}}
    assert move["query"] == {"@microsoft.graph.conflictBehavior": "fail"}
    assert row["item_id"] == "i1"


async def test_a_move_into_a_file_is_refused(graph) -> None:
    graph.answers["GET /drives/d1/root:/a.docx"] = (200, {"id": "x", "file": {}})
    with pytest.raises(sharepoint.GraphError, match="not a folder"):
        await _sp(graph).move_item("d1", "i1", "a.docx")
    assert all(r["key"].startswith("GET") for r in graph.requests)


async def test_a_move_to_the_top_of_the_library(graph) -> None:
    graph.answers["GET /drives/d1/root"] = (200, {"id": "root", "folder": {}})
    await _sp(graph).move_item("d1", "i1", "")
    assert graph.requests[1]["body"] == {"parentReference": {"id": "root"}}


async def test_a_rename_keeps_the_extension_and_never_replaces(graph) -> None:
    graph.answers["GET /drives/d1/items/i1"] = (200, {"name": "Plan v1.docx", "file": {}})
    await _sp(graph).rename_item("d1", "i1", "Plan final")
    rename = graph.requests[1]
    assert rename["key"] == "PATCH /drives/d1/items/i1"
    assert rename["body"] == {"name": "Plan final.docx"}
    assert rename["query"] == {"@microsoft.graph.conflictBehavior": "fail"}


async def test_a_folder_is_renamed_as_given_and_a_path_is_no_name(graph) -> None:
    graph.answers["GET /drives/d1/items/f"] = (200, {"name": "Old.Stuff", "folder": {}})
    await _sp(graph).rename_item("d1", "f", "New")
    assert graph.requests[1]["body"] == {"name": "New"}
    with pytest.raises(sharepoint.GraphError, match="not a name"):
        await _sp(graph).rename_item("d1", "f", "a/b")


async def test_list_items_show_their_own_fields(graph) -> None:
    graph.answers["GET /sites/s1/lists/l1/items"] = (
        200,
        {"value": [{"id": "1", "fields": {"Title": "A", "@odata.etag": "x", "_UIVersion": 1}}]},
    )
    rows = json.loads(await _sp(graph).list_list_items("s1", "l1"))
    assert rows == [{"item_id": "1", "fields": {"Title": "A"}, "link": None}]


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
    assert await _tools({"read_only": True}) == sorted(sharepoint.READING)
    assert await _tools({}) == sorted(sharepoint.READING + sharepoint.WRITING)
