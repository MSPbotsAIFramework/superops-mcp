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
Offset = Annotated[
    int | None,
    Field(description="Record position to start from (0-based, overrides page); pass next_offset from the previous result.", ge=0),
]
# Older clients page with max + cursor; both map onto page / pageSize.
Max = Annotated[
    int | None, Field(description=f"Maximum results; alias of pageSize (values above {MAX_PAGE_SIZE} are capped).", ge=1, le=500)
]
Cursor = Annotated[str | None, Field(description='Page number to fetch, as a string, e.g. "2"; alias of page.')]
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


def locate(offset: int | None, page: int | None, page_size: int | None) -> tuple[int, int, int]:
    """(page, pageSize, start) for a read from record ``start``.

    SuperOps pages by number only, so a position is reached by asking for the page that
    holds it (same page size) and skipping the records before it. Without an offset this is
    the page / pageSize ``list_input`` sends anyway.
    """
    size = page_size or DEFAULT_PAGE_SIZE
    start = offset if offset is not None else ((page or 1) - 1) * size
    return start // size + 1, size, start


def page_result(result: Any, key: str, number: int, size: int, start: int) -> Any:
    """``result[key]`` from record ``start`` of page ``number``, plus ``offset`` / ``next_offset``.

    listInfo is kept as SuperOps returned it. next_offset continues right after the last record
    returned, also when the result is cut to fit the size limit (a page number cannot: the rest
    of a cut page is not on any other page).
    """
    if not isinstance(result, dict) or not isinstance(result.get(key), list):
        return result
    first = (number - 1) * size
    items = result[key]
    info = result.get("listInfo") if isinstance(result.get("listInfo"), dict) else {}
    total = info.get("totalCount")
    # hasMore is null (not false) on the last page.
    more = info.get("hasMore") is True or (isinstance(total, int) and first + len(items) < total)
    kept = items[start - first :]
    return {
        **result,
        key: kept,
        "offset": start,
        "next_offset": start + len(kept) if more else None,
        "_resume": {"key": "next_offset", "start": start},
    }


def text(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None


def paging(
    page: int | None, page_size: int | None, cursor: str | None = None, max_results: int | None = None
) -> tuple[int | None, int | None]:
    """Fold the max / cursor aliases into page / pageSize; raises ValueError when they disagree."""
    if text(cursor):
        raw = text(cursor) or ""
        if not raw.isdigit() or int(raw) < 1:
            raise ValueError('cursor must be a page number from 1, e.g. "2".')
        if page is not None and page != int(raw):
            raise ValueError("page and cursor disagree; pass only one of them.")
        page = int(raw)
    if max_results is not None:
        capped = min(max_results, MAX_PAGE_SIZE)
        if page_size is not None and page_size != capped:
            raise ValueError("pageSize and max disagree; pass only one of them.")
        page_size = capped
    return page, page_size


def alias(name: str, value: str | None, old_name: str, old_value: str | None) -> str | None:
    """One value given under either of two parameter names; raises ValueError when both differ."""
    new, old = text(value), text(old_value)
    if new and old and new != old:
        raise ValueError(f"{name} and {old_name} disagree; pass only one of them.")
    return new or old


def match_by_name(items: list[dict], name: str, kind: str, id_key: str) -> dict:
    """The one item whose name equals ``name`` (case-insensitive); ValueError when none or several match."""
    wanted = name.strip().casefold()
    hits = [i for i in items if isinstance(i, dict) and str(i.get("name") or "").strip().casefold() == wanted]
    if len(hits) == 1:
        return hits[0]
    if hits:
        ids = ", ".join(f"{h.get('name')} ({h.get(id_key)})" for h in hits)
        raise ValueError(f"Several {kind}s are named '{name.strip()}': {ids}. Pass the id instead.")
    names = sorted({str(i.get("name")) for i in items if isinstance(i, dict) and i.get("name")})
    listed = ", ".join(names[:50]) + (" ..." if len(names) > 50 else "")
    raise ValueError(f"No {kind} named '{name.strip()}'. Known: {listed or 'none'}.")


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
    if client.header_error:
        return error_envelope("invalid_argument", client.header_error, False)
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
