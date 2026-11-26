# sharepoint: SharePoint and OneDrive through Microsoft Graph

A code connector for the SharePoint sites, document libraries and lists, and the
OneDrive, of a Microsoft 365 account. It calls
[Microsoft Graph](https://learn.microsoft.com/en-us/graph/api/resources/sharepoint)
directly: no Copilot license, no container of its own.

Every user signs in with their own work account on the Connectors page, and a turn
hands the server only that user's access token (`AIOS_ACCESS_TOKEN`), so an agent
sees exactly what the user may see. The server stores nothing.

| file | what |
|---|---|
| `connector.yaml` | the manifest: approvals for every write, settings (`read_only`, limits) |
| `server.py` | the MCP server: 7 tools that read, 5 that write |
| `agents/sharepoint.md` | an agent for it, added with the connector |
| `tests/` | its tests, against a fake Graph |

## Tools

Reading: `search` (files by name and content, sites, list items; Microsoft Search
syntax), `get_site`, `list_libraries`, `list_folder`, `read_file`, `list_lists`,
`list_list_items`.

Writing, each approved by a human first: `upload_text_file`, `move_item` (within one
library), `rename_item`, `create_list_item`, `update_list_item`. None of them replaces
an existing item unless asked to (only `upload_text_file` can be). With the setting `read_only` (locked: only an administrator
changes it) none of these is offered.

`read_file` reads as text:

- PDF, page by page, with [pypdf](https://pypi.org/project/pypdf/), which ai-os brings
  along. A scanned PDF has no text; the agent is told so.
- Word, Excel (every sheet, tab-separated) and PowerPoint (every slide), also with
  macros (`.docm`, `.xlsm`, `.pptm`), with Python's standard library.
- The old `.doc`, `.xls`, `.ppt`, OpenDocument, RTF and mails (`.msg`, `.eml`): Microsoft
  turns them into a PDF first (`/content?format=pdf`).
- Text files (`.txt`, `.md`, `.csv`, `.json`, code, subtitles, …).

A picture (PNG, JPEG, GIF, WebP, up to 5 MB) goes to the agent as the picture itself,
so the model sees it. Other kinds give only their details and link. Files above
`max_file_mb` are not downloaded. PDFs need an ai-os from 2026-11-26 or later.

## The connector

```yaml
config:
  code: sharepoint
  oauth:
    authorization_endpoint: https://login.microsoftonline.com/${M365_TENANT_ID}/oauth2/v2.0/authorize
    token_endpoint: https://login.microsoftonline.com/${M365_TENANT_ID}/oauth2/v2.0/token
    client_id: ${M365_CLIENT_ID}
    client_secret: ${M365_CLIENT_SECRET}
    scopes: [User.Read, Sites.ReadWrite.All, Files.ReadWrite.All, offline_access]
sign_in: per_user
```

For read only, `Sites.Read.All` and `Files.Read.All` are enough. These are delegated
permissions: the `.All` means every site *the user* can open, not the whole tenant.

Needs ai-os with sign-in for code connectors (`AIOS_ACCESS_TOKEN`).
