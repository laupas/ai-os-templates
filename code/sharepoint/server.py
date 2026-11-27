"""SharePoint: sites, document libraries, lists and OneDrive of the signed-in
Microsoft 365 account, through Microsoft Graph.

ai-os starts it as an MCP session over stdio, for a turn. AIOS_ACCESS_TOKEN is the
access token of the turn's sign-in (the connector's `oauth:` client; with "each
user's own account", the user's own). The server keeps nothing: no token, no file.

Word, Excel and PowerPoint files are read as text with the standard library (they
are zipped XML), PDFs with pypdf (which ai-os brings along). The old Office formats,
OpenDocument, RTF and mails are turned into a PDF by Microsoft first. Pictures go to
the agent as they are, so the model looks at them itself; so do the pages of a PDF
that carry no text (scans), drawn with pypdfium2: the model reads them, which is the
OCR.
"""

import asyncio
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import aiohttp
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

try:
    from pypdf import PdfReader
except ImportError:  # an ai-os from before it brought pypdf along
    PdfReader = None  # type: ignore[assignment, misc]

try:
    import pypdfium2
    import pypdfium2.raw as pdfium_raw
except ImportError:  # an ai-os from before it brought pypdfium2 along
    pypdfium2 = None  # type: ignore[assignment]

DEFAULTS = {
    "read_only": False,
    "max_results": 25,
    "max_file_mb": 20,
    "max_text_chars": 40000,
    "max_scanned_pages": 10,
}
SETTINGS = DEFAULTS | json.loads(os.environ.get("AIOS_CONNECTOR_SETTINGS") or "{}")
# Only the tests set this: ai-os passes a code connector nothing it does not declare.
GRAPH = os.environ.get("SHAREPOINT_GRAPH_URL") or "https://graph.microsoft.com/v1.0"
TIMEOUT = aiohttp.ClientTimeout(total=120)
TEXT_TYPES = (
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".yaml", ".yml",
    ".log", ".ini", ".toml", ".sql", ".py", ".js", ".ts", ".ps1", ".sh", ".vtt", ".srt",
)  # fmt: skip
OFFICE_TYPES = (".docx", ".docm", ".pptx", ".pptm", ".xlsx", ".xlsm")
CONVERTED_TYPES = (
    ".doc", ".xls", ".ppt", ".pps", ".ppsx", ".rtf", ".odt", ".ods", ".odp", ".msg", ".eml",
)  # fmt: skip
"""Read after Microsoft turned them into a PDF (`/content?format=pdf`)."""
IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}
IMAGE_MB = 5
"""The largest picture the model takes, in MB."""
PAGE_PIXELS = 1568
"""The long edge of a scanned page as a picture: the most the model looks at in full."""
UNZIPPED_LIMIT = 200 * 1024 * 1024
"""The most an Office file may unpack to: a guard against zip bombs."""

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class GraphError(ToolError):
    """What Graph refused, in words an agent can pass on. A ToolError, or the MCP
    server keeps the words to itself and the agent sees only that the tool failed."""


class Graph:
    """Calls Microsoft Graph as the signed-in user."""

    def __init__(self, token: str | None, base: str = GRAPH) -> None:
        self._token = token
        self._base = base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise GraphError("not signed in to Microsoft: sign in on the Connectors page")
        return {"Authorization": f"Bearer {self._token}"}

    async def call(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: Any = None,
        data: bytes | None = None,
    ) -> dict[str, Any]:
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
        async with (
            aiohttp.ClientSession(timeout=TIMEOUT) as session,
            session.request(
                method, self._base + path, params=params, json=body, data=data, headers=headers
            ) as response,
        ):
            return self._answer(response.status, await response.read())

    async def download(self, path: str, limit: int) -> bytes:
        """A file's bytes. Graph answers with a redirect to a pre-signed address, which
        is fetched without the token: it never leaves for another host."""
        headers = self._headers()
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(
                self._base + path, headers=headers, allow_redirects=False
            ) as response:
                if response.status not in (301, 302, 303, 307, 308):
                    raw = await self._read(response, limit)
                    if response.status >= 400:
                        self._answer(response.status, raw)
                    return raw
                location = response.headers["Location"]
            async with session.get(location) as response:
                if response.status >= 400:
                    raise GraphError(f"the download answered {response.status}")
                return await self._read(response, limit)

    @staticmethod
    async def _read(response: aiohttp.ClientResponse, limit: int) -> bytes:
        """The body, up to limit + 1 bytes, so a caller can tell it was larger.
        `content.read(n)` alone returns only what has arrived so far."""
        data = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            data += chunk
            if len(data) > limit:
                break
        return bytes(data[: limit + 1])

    @staticmethod
    def _answer(status: int, raw: bytes) -> dict[str, Any]:
        data = json.loads(raw) if raw.strip() else {}
        if status == 401:
            raise GraphError("Microsoft refused the sign-in (401): sign in again")
        if status >= 400:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            message = error.get("message") or raw.decode(errors="replace")[:300]
            raise GraphError(f"Graph answered {status}: {message}")
        return data if isinstance(data, dict) else {}


def _id(value: str) -> str:
    return quote(value, safe="")


def _path(value: str) -> str:
    return quote(value.strip("/"), safe="/")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _limit(limit: int | None) -> int:
    most = int(SETTINGS["max_results"])
    return max(1, min(limit or most, most))


def _cut(text: str) -> str:
    limit = int(SETTINGS["max_text_chars"])
    return text if len(text) <= limit else text[:limit] + f"\n[cut off at {limit} characters]"


def _by(item: dict) -> str | None:
    return ((item.get("lastModifiedBy") or {}).get("user") or {}).get("displayName")


def _item_row(item: dict) -> dict:
    parent = item.get("parentReference") or {}
    return {
        "name": item.get("name"),
        "kind": "folder" if "folder" in item else "file",
        "item_id": item.get("id"),
        "drive_id": parent.get("driveId"),
        "size": item.get("size"),
        "modified": (item.get("lastModifiedDateTime") or "")[:16] or None,
        "modified_by": _by(item),
        "link": item.get("webUrl"),
    }


# Office files as text


def _unzip(content: bytes) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(io.BytesIO(content))
    if sum(i.file_size for i in archive.infolist()) > UNZIPPED_LIMIT:
        raise GraphError("the file unpacks to more than 200 MB; not read")
    return archive


def _xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    return ElementTree.fromstring(archive.read(name))


def _numbered(names: list[str], pattern: str) -> list[str]:
    found = [(int(m.group(1)), n) for n in names if (m := re.fullmatch(pattern, n))]
    return [n for _, n in sorted(found)]


def docx_text(content: bytes) -> str:
    body = _xml(_unzip(content), "word/document.xml")
    lines = []
    for paragraph in body.iter(f"{W}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{W}tab":
                parts.append("\t")
            elif node.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines).strip()


def pptx_text(content: bytes) -> str:
    archive = _unzip(content)
    slides = []
    for number, name in enumerate(
        _numbered(archive.namelist(), r"ppt/slides/slide(\d+)\.xml"), start=1
    ):
        paragraphs = [
            "".join(t.text or "" for t in p.iter(f"{A}t"))
            for p in _xml(archive, name).iter(f"{A}p")
        ]
        text = "\n".join(p for p in paragraphs if p.strip())
        slides.append(f"--- Slide {number}\n{text}")
    return "\n\n".join(slides)


def xlsx_text(content: bytes) -> str:
    archive = _unzip(content)
    names = archive.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        for item in _xml(archive, "xl/sharedStrings.xml").iter(f"{S}si"):
            shared.append("".join(t.text or "" for t in item.iter(f"{S}t")))
    targets = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in _xml(archive, "xl/_rels/workbook.xml.rels").iter(f"{REL}Relationship"):
            targets[rel.get("Id")] = "xl/" + rel.get("Target", "").lstrip("/").removeprefix("xl/")
    sheets = [
        (s.get("name"), targets.get(s.get(f"{R}id")))
        for s in _xml(archive, "xl/workbook.xml").iter(f"{S}sheet")
    ]
    parts = []
    for title, target in sheets:
        if not target or target not in names:
            continue
        rows = []
        for row in _xml(archive, target).iter(f"{S}row"):
            cells = []
            for cell in row.iter(f"{S}c"):
                kind, value = cell.get("t"), cell.find(f"{S}v")
                if kind == "s" and value is not None and value.text:
                    cells.append(shared[int(value.text)])
                elif kind == "inlineStr":
                    cells.append("".join(t.text or "" for t in cell.iter(f"{S}t")))
                else:
                    cells.append(value.text if value is not None and value.text else "")
            if any(cells):
                rows.append("\t".join(cells))
        parts.append(f"--- Sheet {title}\n" + "\n".join(rows))
    return "\n\n".join(parts)


def pdf_pages(content: bytes) -> list[str]:
    """The text of each page."""
    if PdfReader is None:
        raise GraphError("this ai-os cannot read PDFs yet: update it")
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted and not reader.decrypt(""):
        raise GraphError("the PDF is protected by a password")
    return [(page.extract_text() or "").strip() for page in reader.pages]


def scanned(pages: list[str]) -> list[int]:
    """The numbers of the pages without any text: scans."""
    return [n for n, text in enumerate(pages, start=1) if not text]


def png(width: int, height: int, stride: int, pixels: bytes) -> bytes:
    """A grayscale PNG, with the standard library only."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\0" + pixels[y * stride : y * stride + width] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def page_pictures(content: bytes, numbers: list[int]) -> list[bytes]:
    """The given pages as grayscale PNGs, their long edge PAGE_PIXELS, or half that
    where a page would be larger than the model takes."""
    document = pypdfium2.PdfDocument(content)
    try:
        pictures = []
        for number in numbers:
            page = document[number - 1]
            scale = PAGE_PIXELS / max(page.get_size())
            for factor in (1.0, 0.5):
                bitmap = page.render(
                    scale=scale * factor,
                    grayscale=True,
                    force_bitmap_format=pdfium_raw.FPDFBitmap_Gray,
                )
                picture = png(bitmap.width, bitmap.height, bitmap.stride, bytes(bitmap.buffer))
                if len(picture) <= IMAGE_MB * 1024 * 1024:
                    break
            pictures.append(picture)
        return pictures
    finally:
        document.close()


def read_pdf(name: str, content: bytes) -> tuple[str, dict[str, Any], list[bytes]]:
    """A PDF's text, what to tell about its scanned pages, and those pages as
    pictures, up to `max_scanned_pages`."""
    try:
        pages = pdf_pages(content)
        numbers = scanned(pages)
        most = int(SETTINGS["max_scanned_pages"])
        shown = numbers[:most] if pypdfium2 is not None else []
        pictures = page_pictures(content, shown) if shown else []
    except GraphError:
        raise
    except Exception as exc:  # a broken or odd file must not end the session
        raise GraphError(f"{name} could not be read: {exc}") from exc
    text = "\n\n".join(
        f"--- Page {n}\n" + ("[scanned: its picture follows]" if n in shown else t)
        for n, t in enumerate(pages, start=1)
    ).strip()
    notes: dict[str, Any] = {}
    if shown:
        notes["pictures_of_pages"] = shown
    if hidden := [n for n in numbers if n not in shown]:
        notes["scanned_pages_not_shown"] = hidden
        notes["why"] = (
            "this ai-os cannot show scanned pages yet: update it"
            if pypdfium2 is None
            else f"only the first {most} scanned pages are shown"
        )
    return text, notes, pictures


def text_of(name: str, content: bytes) -> str | None:
    """A file's text, or None for a kind that is not read."""
    lower = name.lower()
    try:
        if lower.endswith((".docx", ".docm")):
            return docx_text(content)
        if lower.endswith((".pptx", ".pptm")):
            return pptx_text(content)
        if lower.endswith((".xlsx", ".xlsm")):
            return xlsx_text(content)
    except GraphError:
        raise
    except Exception as exc:  # a broken or odd file must not end the session
        raise GraphError(f"{name} could not be read: {exc}") from exc
    if lower.endswith(TEXT_TYPES):
        return content.decode("utf-8", errors="replace")
    return None


class SharePoint:
    """The tools. Each sees only what the signed-in user may see in SharePoint."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    # Reading

    async def search(
        self,
        query: str,
        kind: Literal["files", "sites", "list_items"] = "files",
        limit: int | None = None,
    ) -> str:
        """Searches SharePoint and OneDrive like the search box does: files (by name
        and content), sites, or list items. Words, "quoted phrases", and properties
        such as filetype:docx, author:"Ada", site:https://….sharepoint.com/sites/x."""
        entity = {"files": "driveItem", "sites": "site", "list_items": "listItem"}[kind]
        data = await self._graph.call(
            "POST",
            "/search/query",
            body={
                "requests": [
                    {
                        "entityTypes": [entity],
                        "query": {"queryString": query},
                        "from": 0,
                        "size": _limit(limit),
                    }
                ]
            },
        )
        rows = []
        for answer in data.get("value", []):
            for container in answer.get("hitsContainers", []):
                for hit in container.get("hits", []):
                    resource = hit.get("resource") or {}
                    row = (
                        _item_row(resource)
                        if entity == "driveItem"
                        else {
                            "name": resource.get("displayName") or resource.get("name"),
                            "id": resource.get("id"),
                            "site_id": (resource.get("parentReference") or {}).get("siteId"),
                            "list_id": (resource.get("sharepointIds") or {}).get("listId"),
                            "link": resource.get("webUrl"),
                        }
                    )
                    summary = re.sub(r"</?c0>|<ddd/>", "", hit.get("summary") or "")
                    rows.append({**row, "summary": summary.strip() or None})
        return _json(rows)

    async def get_site(self, site: str) -> str:
        """A site by its address (https://contoso.sharepoint.com/sites/team) or id:
        its id, name and link, which the other tools take."""
        found = await self._graph.call("GET", self._site_path(site))
        return _json(
            {
                "site_id": found.get("id"),
                "name": found.get("displayName") or found.get("name"),
                "description": found.get("description"),
                "link": found.get("webUrl"),
            }
        )

    async def list_libraries(self, site_id: str) -> str:
        """A site's document libraries, each a drive with its drive_id."""
        data = await self._graph.call(
            "GET", f"/sites/{_id(site_id)}/drives", {"$select": "id,name,description,webUrl"}
        )
        return _json(
            [
                {"drive_id": d.get("id"), "name": d.get("name"), "link": d.get("webUrl")}
                for d in data.get("value", [])
            ]
        )

    async def list_folder(
        self, drive_id: str | None = None, path: str = "", limit: int | None = None
    ) -> str:
        """Files and folders in a library (drive_id from list_libraries or search) at a
        path such as `Projects/2026`; without drive_id, the user's own OneDrive."""
        drive = f"/drives/{_id(drive_id)}" if drive_id else "/me/drive"
        where = f"/root:/{_path(path)}:/children" if path.strip("/") else "/root/children"
        data = await self._graph.call("GET", drive + where, {"$top": str(_limit(limit))})
        return _json([_item_row(i) for i in data.get("value", [])])

    async def read_file(self, drive_id: str, item_id: str) -> list[str | Image]:
        """A file's content. As text: PDF (page by page), Word, Excel (every sheet,
        tab-separated), PowerPoint (every slide), the old .doc/.xls/.ppt, OpenDocument,
        RTF, mails (.msg, .eml) and plain text files. A picture (PNG, JPEG, GIF, WebP)
        comes as the picture itself, and so does each scanned page of a PDF (listed in
        `pictures_of_pages`): read them yourself, and say what you could not make out.
        Other kinds give only their details and link."""
        path = f"/drives/{_id(drive_id)}/items/{_id(item_id)}"
        item = await self._graph.call("GET", path)
        row = _item_row(item)
        if "folder" in item:
            raise GraphError(f"{item.get('name')} is a folder: use list_folder")
        name = item.get("name") or ""
        lower = name.lower()
        picture = next((f for e, f in IMAGE_TYPES.items() if lower.endswith(e)), None)
        mb = (
            min(int(SETTINGS["max_file_mb"]), IMAGE_MB) if picture else int(SETTINGS["max_file_mb"])
        )
        limit = mb * 1024 * 1024
        if (item.get("size") or 0) > limit:
            return [_json({**row, "text": None, "why": f"larger than {mb} MB"})]
        converted = lower.endswith(CONVERTED_TYPES)
        if not (picture or converted or lower.endswith((".pdf", *OFFICE_TYPES, *TEXT_TYPES))):
            return [_json({**row, "text": None, "why": "this kind of file is not read"})]
        content = await self._graph.download(
            f"{path}/content" + ("?format=pdf" if converted else ""), limit
        )
        if len(content) > limit:
            return [_json({**row, "text": None, "why": f"larger than {mb} MB"})]
        if picture:
            return [_json(row), Image(data=content, format=picture)]
        if lower.endswith((".pdf", *CONVERTED_TYPES)):
            text, notes, pages = await asyncio.to_thread(read_pdf, name, content)
            if not pages and not re.sub(r"--- Page \d+", "", text).strip():
                notes.setdefault("why", "the PDF has no text: scanned pages")
                return [_json({**row, "text": None, **notes})]
            return [
                _json({**row, "text": _cut(text), **notes}),
                *(Image(data=p, format="png") for p in pages),
            ]
        text = (await asyncio.to_thread(text_of, name, content) or "").strip()
        return [_json({**row, "text": _cut(text)})]

    async def list_lists(self, site_id: str) -> str:
        """A site's lists (not its libraries), with their list_id."""
        data = await self._graph.call(
            "GET",
            f"/sites/{_id(site_id)}/lists",
            {"$select": "id,displayName,description,webUrl,list"},
        )
        return _json(
            [
                {
                    "list_id": entry.get("id"),
                    "name": entry.get("displayName"),
                    "description": entry.get("description") or None,
                    "link": entry.get("webUrl"),
                }
                for entry in data.get("value", [])
                if not (entry.get("list") or {}).get("hidden")
                and (entry.get("list") or {}).get("template") != "documentLibrary"
            ]
        )

    async def list_list_items(self, site_id: str, list_id: str, limit: int | None = None) -> str:
        """A list's items with their fields (column values by internal name)."""
        data = await self._graph.call(
            "GET",
            f"/sites/{_id(site_id)}/lists/{_id(list_id)}/items",
            {"$expand": "fields", "$top": str(_limit(limit))},
        )
        return _json(
            [
                {
                    "item_id": i.get("id"),
                    "fields": {
                        k: v
                        for k, v in (i.get("fields") or {}).items()
                        if not k.startswith(("@", "_")) and k not in ("ContentType", "Edit")
                    },
                    "link": i.get("webUrl"),
                }
                for i in data.get("value", [])
            ]
        )

    # Writing

    async def upload_text_file(
        self, drive_id: str, folder: str, name: str, content: str, replace: bool = False
    ) -> str:
        """Saves a text file (.txt, .md, .csv, …) in a library folder; an existing file
        of that name is replaced only with `replace`. A human approves it first."""
        where = "/".join(p for p in (_path(folder), _path(name)) if p)
        uploaded = await self._graph.call(
            "PUT",
            f"/drives/{_id(drive_id)}/root:/{where}:/content",
            {"@microsoft.graph.conflictBehavior": "replace" if replace else "fail"},
            data=content.encode(),
        )
        return _json(_item_row(uploaded))

    async def move_item(self, drive_id: str, item_id: str, folder: str) -> str:
        """Moves a file or folder to another folder of the same library, given by its
        path such as `Projects/2026` ("" for the top). It fails if the folder already
        holds an item of that name. Another library is not possible: Graph moves only
        within one. A human approves it first."""
        drive = f"/drives/{_id(drive_id)}"
        target = await self._graph.call(
            "GET", f"{drive}/root:/{_path(folder)}" if folder.strip("/") else f"{drive}/root"
        )
        if "folder" not in target:
            raise GraphError(f"{folder} is not a folder")
        moved = await self._graph.call(
            "PATCH",
            f"{drive}/items/{_id(item_id)}",
            {"@microsoft.graph.conflictBehavior": "fail"},
            body={"parentReference": {"id": target.get("id")}},
        )
        return _json(_item_row(moved))

    async def rename_item(self, drive_id: str, item_id: str, name: str) -> str:
        """Renames a file or folder where it is. A file keeps its extension when `name`
        has none. It fails if the folder already holds an item of that name. A human
        approves it first."""
        path = f"/drives/{_id(drive_id)}/items/{_id(item_id)}"
        item = await self._graph.call("GET", path, {"$select": "id,name,file,folder"})
        old, new = item.get("name") or "", name.strip()
        if not new or "/" in new or "\\" in new:
            raise GraphError(f"{name!r} is not a name")
        if "file" in item and "." in old and "." not in new:
            new += old[old.rindex(".") :]
        renamed = await self._graph.call(
            "PATCH", path, {"@microsoft.graph.conflictBehavior": "fail"}, body={"name": new}
        )
        return _json(_item_row(renamed))

    async def create_list_item(self, site_id: str, list_id: str, fields: dict[str, Any]) -> str:
        """Adds an item to a list; `fields` by internal column name, as
        list_list_items shows them. A human approves it first."""
        created = await self._graph.call(
            "POST", f"/sites/{_id(site_id)}/lists/{_id(list_id)}/items", body={"fields": fields}
        )
        return _json({"item_id": created.get("id"), "link": created.get("webUrl")})

    async def update_list_item(
        self, site_id: str, list_id: str, item_id: str, fields: dict[str, Any]
    ) -> str:
        """Changes the given fields of a list item; the others stay. A human approves
        it first."""
        changed = await self._graph.call(
            "PATCH",
            f"/sites/{_id(site_id)}/lists/{_id(list_id)}/items/{_id(item_id)}/fields",
            body=fields,
        )
        return _json({k: v for k, v in changed.items() if not k.startswith("@")})

    @staticmethod
    def _site_path(site: str) -> str:
        if not site.startswith("https://"):
            return f"/sites/{_id(site)}"
        parts = urlsplit(site)
        path = parts.path.strip("/")
        return f"/sites/{parts.hostname}:/{_path(path)}" if path else f"/sites/{parts.hostname}"


READING = [
    "search",
    "get_site",
    "list_libraries",
    "list_folder",
    "read_file",
    "list_lists",
    "list_list_items",
]
WRITING = ["upload_text_file", "move_item", "rename_item", "create_list_item", "update_list_item"]


def server(sharepoint: SharePoint) -> MCPServer:
    mcp = MCPServer("sharepoint")
    for name in READING + ([] if SETTINGS["read_only"] else WRITING):
        mcp.tool()(getattr(sharepoint, name))
    return mcp


if __name__ == "__main__":
    if sys.argv[1:2]:
        raise SystemExit(f"unknown command {sys.argv[1]}")
    server(SharePoint(Graph(os.environ.get("AIOS_ACCESS_TOKEN")))).run()
