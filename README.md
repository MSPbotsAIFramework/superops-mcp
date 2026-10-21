# SuperOps.ai MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [SuperOps.ai](https://superops.com)
MSP GraphQL API, the combined PSA and RMM. It lets an AI agent read client accounts, tickets,
managed assets (with software and patches) and technicians, run read-only GraphQL queries and,
on explicit request, create and update tickets, add notes, log time and run custom mutations.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: the API token arrives on every request as an HTTP header and is
  never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx, graphql-core
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-SuperOps-API-Token` | Yes | string | none | - | SuperOps API token (a leading `Bearer ` is accepted and ignored) | `your-api-token` |
| `X-SuperOps-Subdomain` | Yes | string | none | subdomain | Tenant subdomain; `yourcompany.superops.ai` or its URL is also accepted | `yourcompany` |
| `X-SuperOps-Region` | No | string | `us` | `us`, `eu` | Data-centre region of the tenant | `us` |

Where to get them:

- Sign in to SuperOps, open the settings icon -> **My Profile -> API token** and click
  **Generate token**. The token acts with that user's permissions, so use an account whose
  role allows what the agent should do.
- The subdomain is the first part of the tenant's address (`yourcompany` in
  `yourcompany.superops.ai`).
- EU tenants use `X-SuperOps-Region: eu` (endpoint `euapi.superops.ai`); everyone else can omit it.

How credentials are sent to SuperOps: every call is a GraphQL `POST` to
`https://api.superops.ai/msp` (or `https://euapi.superops.ai/msp`) with
`Authorization: Bearer <token>` and `CustomerSubDomain: <subdomain>`. Values that look like
unresolved templates (`${...}`) are treated as missing.

A request to `/mcp` without the token or subdomain gets HTTP 401 naming the missing header. A
malformed subdomain or unknown region does not block `tools/list`; tools then return an
`invalid_argument` error describing the bad header.

## Tools

All tools return a JSON string. Lists take `page` (from 1) and `pageSize` (default 50,
maximum 100) and return SuperOps' `listInfo` `{page, pageSize, totalCount, hasMore}`;
`hasMore` is `null`, not `false`, on the last page.

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `superops_navigate` | Describe the tools of one area (changes nothing) | **domain** (`clients`, `tickets`, `assets`, `technicians`, `custom`) |
| `superops_status` | Show the tenant and region this connection targets (no API call) | - |
| `superops_test_connection` | Test the token and subdomain; returns the tenant's client count | - |
| `superops_clients_list` | List client accounts, optionally by stage and status | status, stage, page, pageSize, max, cursor |
| `superops_clients_get` | Get one client account with contacts, HQ site, groups and custom fields | **accountId** |
| `superops_clients_search` | Search clients by part of the name or an email domain | **query**, page, pageSize, max |
| `superops_tickets_list` | List tickets, newest first | status (list), priority (list), clientId, technicianId, assigneeId, unassigned, page, pageSize, max, cursor |
| `superops_tickets_get` | Get one ticket with SLA times, worklog total and custom fields | **ticketId** |
| `superops_tickets_create` | Create a ticket (**write**) | **subject**, **clientId**, description, source, status, priority, impact, urgency, category, subcategory, requestType, siteId, requesterId, techGroupId, technicianId, categoryName, techGroupName, requesterEmail |
| `superops_tickets_update` | Change status, priority, classification or assignee (**write**) | **ticketId**, subject, status, priority, impact, urgency, category, subcategory, requestType, resolutionCode, technicianId, techGroupId, assigneeId, techGroupName, resolution |
| `superops_tickets_add_note` | Add a private (default) or public note (**write**) | **ticketId**, **content**, isPublic |
| `superops_tickets_log_time` | Log worked time on a ticket (**write**) | **ticketId**, qty or duration (exactly one), billDateTime, notes, billable, afterHours, technicianId, serviceItemId, unitPrice, description, workType |
| `superops_assets_list` | List managed assets | status, platform, clientId, page, pageSize, max, cursor |
| `superops_assets_get` | Get one asset: hardware, OS, network, agent and patch status | **assetId** |
| `superops_assets_software` | Software installed on an asset | **assetId**, search, page, pageSize, max |
| `superops_assets_patches` | Patches of an asset with approval and install status | **assetId**, installationStatus, status, severity (list), page, pageSize |
| `superops_technicians_list` | List technicians, optionally by name or email | search, teamId, activeOnly, page, pageSize, max, cursor |
| `superops_technicians_get` | Get one technician | **technicianId** |
| `superops_technicians_groups` | List technician groups | max |
| `superops_technicians_lookups` | Roles, teams, designations, business functions and groups with ids | - |
| `superops_custom_query` | Run a read-only GraphQL query | query (required at run time), variables |
| `superops_custom_mutation` | Run a GraphQL mutation (**write**) | **mutation**, variables |

Ticket status, priority, impact, urgency, category and resolution codes are strings configured
per tenant; the stock values are listed in each parameter's description.

### Compatibility parameters

The gateway may still send arguments from the older tool contract, so these are accepted and
mapped (`tests/contract_baseline.json` and `tests/test_contract.py` pin both contracts). When an
old and a new form are both given and disagree, the call fails with `invalid_argument`.

| Parameter | Tools | Maps to |
|---|---|---|
| `max` | list / search tools, `superops_assets_software` | `pageSize` (values above 100 are capped) |
| `max` | `superops_technicians_groups` | limits the returned list locally (`totalCount` shows the full count) |
| `cursor` | list tools | `page`, given as a string, e.g. `"2"` |
| `assigneeId` | `superops_tickets_list`, `superops_tickets_update` | `technicianId` |
| `unassigned` | `superops_tickets_list` | only tickets without a technician |
| `teamId` | `superops_technicians_list` | filter on the technician's team |
| `activeOnly` | `superops_technicians_list` | accepted, no effect (SuperOps has no active flag) |
| `categoryName` | `superops_tickets_create` | `category` |
| `techGroupName` | `superops_tickets_create`, `superops_tickets_update` | `techGroupId`, looked up by exact name (any case) in `getTechnicianGroupList` |
| `requesterEmail` | `superops_tickets_create` | `requesterId`, looked up by exact email among the client's users (`getClientUserList`) |
| `resolution` | `superops_tickets_update` | a private ticket note (`createNote`) in the same call, returned as `resolutionNote` next to `ticket` |
| `duration` | `superops_tickets_log_time` | `qty` in hours (minutes / 60); pass exactly one of `qty` or `duration` |
| `description` | `superops_tickets_log_time` | `notes` |
| `workType` | `superops_tickets_log_time` | `serviceItemId`, looked up by exact name in `getServiceCatalogItemList` |
| `status` | `superops_assets_patches` | `installationStatus` |

Unknown or ambiguous names and emails fail with `invalid_argument` before anything is written.

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "SuperOps returned HTTP 401: Unauthorized", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. GraphQL errors, which SuperOps returns with HTTP 200, are
mapped onto the same codes. Queries are retried with backoff on rate limiting, 5xx and network
errors; mutations are retried only on rate limiting, so a write never runs twice.

## Run locally

```bash
uv sync
uv run python -m superops_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t superops-mcp:dev .
docker run --rm -p 8080:8080 superops-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-SuperOps-API-Token: your-api-token" \
  -H "X-SuperOps-Subdomain: yourcompany" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# open tickets
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-SuperOps-API-Token: your-api-token" \
  -H "X-SuperOps-Subdomain: yourcompany" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"superops_tickets_list","arguments":{"status":["Open"],"pageSize":10}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (22).** Kept as is because the tool names are an existing contract; parameters
  are a superset of the deployed and the upstream contracts (see Compatibility parameters).
- **Schema-checked queries.** Every GraphQL document was validated against the SuperOps MSP
  schema published at developer.superops.com.
- **Read-only custom queries.** `superops_custom_query` parses the document and refuses
  mutations, so a read tool cannot write; writes go through `superops_custom_mutation`, which is
  annotated destructive and never retried automatically.
- **Region.** The optional `X-SuperOps-Region` header reaches EU tenants; without it the US
  endpoint is used as before.
- **Write tools.** Ticket create / update / note / time tools change live records (public notes
  reach the client, billable time can be invoiced); their descriptions tell the agent to use them
  only on explicit request. `qty` and `unitPrice` accept numbers as well as numeric strings.
- **No interactive prompts.** Older versions asked the user for filters before unfiltered lists,
  which a stateless gateway cannot relay; lists now page instead.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
