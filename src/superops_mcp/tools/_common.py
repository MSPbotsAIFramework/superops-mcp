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
"""Shared argument types, condition / paging helpers and the error-handling wrapper."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import SuperOpsClient, SuperOpsError

logger = logging.getLogger("superops_mcp.tools")

ClientFactory = Callable[[], SuperOpsClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
WRITES_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
# Arbitrary GraphQL mutations can delete data.
ANY_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "SuperOps credentials are missing for this request (X-SuperOps-API-Token and X-SuperOps-Subdomain).",
    False,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

Page = Annotated[int | None, Field(description="Page number from 1 (default 1).", ge=1, le=2_147_483_647)]
PageSize = Annotated[
    int | None, Field(description=f"Results per page (default {DEFAULT_PAGE_SIZE}, max {MAX_PAGE_SIZE}).", ge=1, le=MAX_PAGE_SIZE)
]
TENANT_VALUES = "Your tenant may rename or extend this list."
STOCK_STATUSES = "Open, On Hold, Resolved, Closed, Waiting on third party"
STOCK_PRIORITIES = "Critical, High, Medium, Low, Very Low"
STOCK_SEVERITIES = "High, Medium, Low"
STOCK_CATEGORIES = "Database, Hardware, Help, Network, Software"
STOCK_RESOLUTION_CODES = "Permanent Fix, Workaround, Resolved by Requester, Exception"


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def clause(attribute: str, operator: str, value: Any) -> dict:
    return {"attribute": attribute, "operator": operator, "value": value}


def join(operator: str, clauses: list[dict | None]) -> dict | None:
    present = [c for c in clauses if c]
    if len(present) <= 1:
        return present[0] if present else None
    return {"joinOperator": operator, "operands": present}


def list_input(page: int | None, page_size: int | None, condition: dict | None = None, sort: list[dict] | None = None) -> dict:
    return compact({"page": page or 1, "pageSize": page_size or DEFAULT_PAGE_SIZE, "condition": condition, "sort": sort})


def text(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[SuperOpsClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except SuperOpsError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "SuperOps returned an empty result."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
