# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import SuperOpsClient, SuperOpsCredentials
from .config import API_ENDPOINTS, DEFAULT_REGION, Settings

HEADER_API_TOKEN = "X-SuperOps-API-Token"
HEADER_SUBDOMAIN = "X-SuperOps-Subdomain"
HEADER_REGION = "X-SuperOps-Region"
REQUIRED_HEADERS = [HEADER_API_TOKEN, HEADER_SUBDOMAIN]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")
_BEARER = re.compile(r"^bearer(\s+|$)", re.IGNORECASE)
_SUBDOMAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_SUBDOMAIN_URL = re.compile(r"^(?:https?://)?([A-Za-z0-9-]+)\.superops\.ai/?.*$", re.IGNORECASE)

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[SuperOpsCredentials | None] = contextvars.ContextVar(
    "superops_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("superops_request_id", default="-")

INSTRUCTIONS = (
    "SuperOps.ai is a combined PSA and RMM for MSPs: clients (customer "
    "accounts), tickets, managed assets (endpoints) and technicians.\n"
    "Usage:\n"
    "- Ids are strings. Find a client with superops_clients_search(query), "
    "then superops_tickets_list(clientId) or superops_assets_list(clientId).\n"
    "- Lists page with page + pageSize (max 100) and return listInfo "
    "{page, pageSize, totalCount, hasMore}; hasMore is null on the last page.\n"
    "- Ticket status, priority, category and resolution codes are tenant "
    "configured strings; use the values the tenant shows.\n"
    "- superops_technicians_lookups turns team / role / group names into ids.\n"
    "- superops_custom_query runs read-only GraphQL for anything else "
    "(e.g. getAssetSummary, getAssetDiskDetails).\n"
    "Creating or updating tickets, adding notes, logging time and "
    "superops_custom_mutation change live records (public notes reach the "
    "client): call them only when the user explicitly asks, with the exact "
    "values given."
)


def get_client_from_context() -> SuperOpsClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return SuperOpsClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


def normalize_subdomain(value: str) -> str | None:
    """The tenant subdomain, also accepted as yourcompany.superops.ai or its URL."""
    text = value.strip()
    match = _SUBDOMAIN_URL.match(text)
    if match:
        text = match.group(1)
    return text.lower() if _SUBDOMAIN.match(text) else None


class GatewayCredentialsMiddleware:
    """Reads the SuperOps headers on /mcp requests; 401 when the token or subdomain is missing.

    A malformed subdomain or region does not block the request (tools/list must work
    with any values); tools report it instead.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        token = _clean(request.headers.get(HEADER_API_TOKEN))
        token = _clean(_BEARER.sub("", token)) if token else None
        raw_subdomain = _clean(request.headers.get(HEADER_SUBDOMAIN))
        if not token or not raw_subdomain:
            missing = [name for name, value in ((HEADER_API_TOKEN, token), (HEADER_SUBDOMAIN, raw_subdomain)) if not value]
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                    "optional_headers": [HEADER_REGION],
                },
                status_code=401,
            )(scope, receive, send)
            return

        subdomain = normalize_subdomain(raw_subdomain)
        region = (_clean(request.headers.get(HEADER_REGION)) or DEFAULT_REGION).lower()
        header_error = None
        if subdomain is None or region not in API_ENDPOINTS:
            header_error = (
                f"{HEADER_SUBDOMAIN} must be the tenant subdomain, e.g. yourcompany"
                if subdomain is None
                else f"{HEADER_REGION} must be one of: {', '.join(API_ENDPOINTS)}"
            )
            # Safe stand-ins; tools return header_error before any API call.
            subdomain = subdomain or "invalid"
            region = region if region in API_ENDPOINTS else DEFAULT_REGION

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        credentials = SuperOpsCredentials(api_token=token, subdomain=subdomain, region=region, header_error=header_error)
        creds_token = _credentials_var.set(credentials)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="superops-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], SuperOpsClient | None] = get_client_from_context

    from .tools import assets, clients, custom, navigation, technicians, tickets

    for module in (navigation, clients, tickets, assets, technicians, custom):
        module.register(mcp, client_factory)

    return mcp
