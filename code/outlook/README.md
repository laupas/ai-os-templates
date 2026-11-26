# outlook: Outlook mail and calendar through Microsoft Graph

A code connector for the mailbox and calendar of a Microsoft 365 account. It calls
[Microsoft Graph](https://learn.microsoft.com/en-us/graph/overview) directly, so it
needs no Copilot license, no Work IQ and no container of its own.

Every user signs in with their own work account on the Connectors page. ai-os keeps
and renews the sign-in, and a turn hands the server only the access token of the user
it is for (`AIOS_ACCESS_TOKEN`). The server stores nothing.

| file | what |
|---|---|
| `connector.yaml` | the manifest: approvals for everything that sends or changes what others see, settings (`read_only`, list and body limits) |
| `server.py` | the MCP server: 8 tools that read, 10 that write |
| `agents/outlook.md` | an agent for it, added with the connector |
| `tests/` | its tests, against a fake Graph |

## Tools

Reading: `whoami`, `list_folders`, `list_messages`, `get_message`, `list_events`,
`get_event`, `free_busy`, `find_people`.

Writing: `create_draft`, `create_reply_draft`, `mark_read`, `move_message`, and,
each approved by a human first, `send_mail`, `send_draft`, `create_event`,
`update_event`, `delete_event`, `respond_to_event`. With the setting `read_only`
(locked: only an administrator changes it) none of these is offered.

## The connector

```yaml
config:
  code: outlook
  oauth:
    authorization_endpoint: https://login.microsoftonline.com/${M365_TENANT_ID}/oauth2/v2.0/authorize
    token_endpoint: https://login.microsoftonline.com/${M365_TENANT_ID}/oauth2/v2.0/token
    client_id: ${M365_CLIENT_ID}
    client_secret: ${M365_CLIENT_SECRET}
    scopes: [User.Read, Mail.ReadWrite, Mail.Send, Calendars.ReadWrite, People.Read, offline_access]
sign_in: per_user
```

The Entra app registration: platform *Web* with the redirect URI
`https://<your ai-os address>/api/oauth/callback`, a client secret, and the Microsoft
Graph delegated permissions above. For read only, `Mail.Read`, `Calendars.Read`,
`People.Read`, `User.Read` and `offline_access` are enough.

Needs ai-os with sign-in for code connectors (`AIOS_ACCESS_TOKEN`).
