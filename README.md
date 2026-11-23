# ai-os templates

Agents, connectors, triggers and a code connector for
[ai-os](https://github.com/laupas/ai-os), to add one at a time from the console's
**Store** page. ai-os lists this repository as a store source by default.

| Kind | Items |
|---|---|
| Connectors | Google Calendar, Gmail, Google Drive, Google Contacts (Google's own MCP servers), Playwright browser, GitHub (read-only), Homey, Hello world (code connector) |
| Agents | calendar, contacts, inbox-triage, drive, browser, github, homey, weather, second-brain |
| Triggers | Morning brief, Weekly inbox summary |

Adding an agent also adds the connectors and skills it uses from here, if you do not
have them yet; the Store page asks for the values they need (an OAuth client, a
token) on the way. What you already have is never overwritten unless you choose to
replace it. Once added, an item is yours: change it in the console like anything
else.

## Using another source

A store source is any git repository laid out like this one. Add it on the Store
page (its https URL, and a branch if not the default), or in `aios.yaml`:

```yaml
store:
  sources:
    - url: https://github.com/laupas/ai-os-templates
    - url: https://github.com/<you>/<your-templates>
      branch: main
```

Only add sources you trust: an agent's prompt decides what it does with the tools it
is granted, and a code connector runs its code inside your installation.

## Layout

The same files as an ai-os configuration repository:

| Path | What |
|---|---|
| `store.yaml` | `name` and `description` of the source |
| `agents/<id>.md` | an agent: YAML front matter (`description`, `model`, `enabled`, `builtin_tools`, `skills`, `connectors`), then the prompt. `connectors` maps a connector id to the tools granted on it, or `null` for all |
| `skills/<id>/SKILL.md` | a skill: `name`, `description`, then the instructions |
| `connectors/<id>.yaml` | an MCP server: `description`, `config` (one server in Claude Code's `.mcp.json` shape), `enabled`, `sign_in` (`shared` or `per_user`), `shared_fallback` |
| `code/<name>/` | a code connector (`connector.yaml`, `server.py`, optional `agents/`), offered through a connector whose config is `{code: <name>}` |
| `triggers/<id>.yaml` | a trigger: `name`, `prompt`, `cron` or `once_at`, `timezone`, `agent`, `enabled`, `max_lateness_minutes`. No user: it runs for whoever adds it |

### The `store:` block

Every item may carry a `store:` block (in the front matter of a `.md` file) that
ai-os uses on the Store page and drops when it adds the item:

```yaml
store:
  title: Google Calendar          # the name shown; default: the id
  category: Google                # grouping on the Store page
  notes: |                        # Markdown shown before adding: setup steps
    Create an OAuth client ...
  variables:                      # asked for when adding, saved as Variables
    - variable: GOOGLE_AIOS_CLIENT_ID
      label: Google OAuth client id
      hint: Where to find it
      secret: false               # true (the default): stored encrypted, never shown
      required: true
  requires: [connectors/google-calendar]   # more items to add with it
```

Requirements an item implies need no listing: an agent needs its connectors and
skills, a trigger its agent.

## Contributing

- One item per file, named by its id (lower-case letters, digits and dashes).
- No secrets and no personal data: every key, token or per-installation value is a
  `${VARIABLE}` with an entry under `store.variables`.
- Give a connector's write tools some thought: ai-os asks for approval according to
  the installation's rules, but a narrow grant on the agent is the first guard.
- Prompts in English, short, and explicit about what the agent must never do.

## License

MIT
