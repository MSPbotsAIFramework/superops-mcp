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
"""Tickets: list, read, create, update, notes and worklog entries."""

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BeforeValidator, Field

from ..api_client import SuperOpsClient, SuperOpsError
from ._common import (
    MAX_PAGE_SIZE,
    READ_ONLY,
    STOCK_CATEGORIES,
    STOCK_PRIORITIES,
    STOCK_RESOLUTION_CODES,
    STOCK_SEVERITIES,
    STOCK_STATUSES,
    TENANT_VALUES,
    WRITES,
    WRITES_IDEMPOTENT,
    ClientFactory,
    Cursor,
    Max,
    Page,
    PageSize,
    alias,
    clause,
    compact,
    join,
    list_input,
    match_by_name,
    paging,
    run_tool,
    text,
)
from .technicians import GROUP_LIST

TICKET_LIST = """
query listTickets($input: ListInfoInput!) {
  getTicketList(input: $input) {
    tickets {
      ticketId displayId subject status priority requestType source category
      createdTime updatedTime client site requester techGroup technician
    }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

TICKET_GET = """
query getTicket($input: TicketIdentifierInput!) {
  getTicket(input: $input) {
    ticketId displayId subject requestType source status approvalStatus priority impact urgency
    category subcategory cause subcause resolutionCode
    createdTime updatedTime firstResponseDueTime firstResponseTime firstResponseViolated
    resolutionDueTime resolutionTime resolutionViolated worklogTimespent
    client site requester techGroup technician sla customFields
  }
}
"""

TICKET_CREATE = """
mutation createTicket($input: CreateTicketInput!) {
  createTicket(input: $input) {
    ticketId displayId subject status priority category requestType source createdTime
    client site requester techGroup technician
  }
}
"""

TICKET_UPDATE = """
mutation updateTicket($input: UpdateTicketInput!) {
  updateTicket(input: $input) {
    ticketId displayId subject status priority impact urgency category subcategory requestType
    resolutionCode updatedTime techGroup technician
  }
}
"""

NOTE_CREATE = """
mutation addNote($input: CreateNoteInput!) {
  createNote(input: $input) { noteId content addedOn addedBy privacyType }
}
"""

WORKLOG_CREATE = """
mutation logTime($input: [CreateWorklogEntryInput!]!) {
  createWorklogEntries(input: $input) {
    itemId status qty unitPrice billable afterHours billDateTime notes serviceItem technician workItem
  }
}
"""

CLIENT_USER_FIND = """
query findClientUser($input: GetClientUserListInput!) {
  getClientUserList(input: $input) {
    userList { userId name email }
  }
}
"""

SERVICE_ITEM_FIND = """
query findServiceItem($input: ListInfoInput!) {
  getServiceCatalogItemList(input: $input) {
    items { itemId name offerAsWorklogItem status }
  }
}
"""

TicketId = Annotated[str, Field(description="Ticket id (ticketId, not the display number).", min_length=1)]
Source = Literal[
    "FORM", "AGENT", "EMAIL", "AI", "PHONE", "INTEGRATION", "SCHEDULE", "CONTRACT_REMINDER", "CONTRACT", "INSTANT_MESSAGING"
]
StatusText = Annotated[str | None, Field(description=f"Status. SuperOps ships with: {STOCK_STATUSES}. {TENANT_VALUES}")]
PriorityText = Annotated[str | None, Field(description=f"Priority. SuperOps ships with: {STOCK_PRIORITIES}. {TENANT_VALUES}")]
ImpactText = Annotated[str | None, Field(description=f"Impact: {STOCK_SEVERITIES}. {TENANT_VALUES}")]
UrgencyText = Annotated[str | None, Field(description=f"Urgency: {STOCK_SEVERITIES}. {TENANT_VALUES}")]
CategoryText = Annotated[str | None, Field(description=f"Service category. SuperOps ships with: {STOCK_CATEGORIES}. {TENANT_VALUES}")]
SubcategoryText = Annotated[str | None, Field(description="Service subcategory, one defined under the category.")]
RequestTypeText = Annotated[str | None, Field(description="Request type, e.g. Incident or Service Request.")]
TechGroupName = Annotated[
    str | None, Field(description="Technician group name (exact, any case); resolved to techGroupId. Alternative to techGroupId.")
]


def _number_text(value: Any) -> Any:
    """Accept 1.5 as well as "1.5" for the string-typed quantity fields."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return format(value, "g")
    return value


def _hours(minutes: float) -> str:
    """Minutes as the hours string SuperOps expects for qty, e.g. 90 -> "1.5"."""
    return format(round(minutes / 60, 4), "f").rstrip("0").rstrip(".") or "0"


async def resolve_tech_group(client: SuperOpsClient, group_id: str | None, group_name: str | None) -> str | None:
    """techGroupId, or the id of the group named group_name; ValueError when unknown, ambiguous or conflicting."""
    if not text(group_name):
        return text(group_id)
    groups = (await client.query(GROUP_LIST)).get("getTechnicianGroupList") or []
    found = str(match_by_name(groups, group_name or "", "technician group", "groupId").get("groupId"))
    if text(group_id) and text(group_id) != found:
        raise ValueError(f"techGroupId {text(group_id)} and techGroupName '{text(group_name)}' (id {found}) disagree.")
    return found


async def resolve_requester(client: SuperOpsClient, client_id: str, requester_id: str | None, email: str | None) -> str | None:
    """requesterId, or the client user id with this email; ValueError when unknown or conflicting."""
    wanted = text(email)
    if not wanted:
        return text(requester_id)
    condition = clause("email", "contains", wanted)
    variables = {"input": {"clientId": client_id, "listInfo": list_input(1, MAX_PAGE_SIZE, condition)}}
    users = ((await client.query(CLIENT_USER_FIND, variables)).get("getClientUserList") or {}).get("userList") or []
    hits = {
        str(u.get("userId"))
        for u in users
        if isinstance(u, dict) and str(u.get("email") or "").strip().casefold() == wanted.casefold()
    }
    if not hits:
        raise ValueError(f"No user with email {wanted} under client {client_id}; check the address or pass requesterId.")
    if len(hits) > 1:
        raise ValueError(f"Several users have email {wanted} ({', '.join(sorted(hits))}); pass requesterId instead.")
    found = hits.pop()
    if text(requester_id) and text(requester_id) != found:
        raise ValueError(f"requesterId {text(requester_id)} and requesterEmail {wanted} (user {found}) disagree.")
    return found


async def resolve_service_item(client: SuperOpsClient, item_id: str | None, work_type: str | None) -> str | None:
    """serviceItemId, or the id of the service catalog item named work_type; ValueError when unknown or conflicting."""
    if not text(work_type):
        return text(item_id)
    variables = {"input": list_input(1, MAX_PAGE_SIZE, clause("name", "contains", text(work_type)))}
    items = ((await client.query(SERVICE_ITEM_FIND, variables)).get("getServiceCatalogItemList") or {}).get("items") or []
    wanted = (text(work_type) or "").casefold()
    exact = [i for i in items if isinstance(i, dict) and str(i.get("name") or "").strip().casefold() == wanted]
    worklog = [i for i in exact if i.get("offerAsWorklogItem")]
    # A name shared by several items is fine when only one of them is offered as a worklog item.
    candidates = worklog if len(exact) > 1 and len(worklog) == 1 else items
    try:
        found = str(match_by_name(candidates, work_type or "", "service item", "itemId").get("itemId"))
    except ValueError as e:
        raise ValueError(f"workType must name a service catalog item (or pass serviceItemId). {e}") from e
    if text(item_id) and text(item_id) != found:
        raise ValueError(f"serviceItemId {text(item_id)} and workType '{text(work_type)}' (item {found}) disagree.")
    return found


def assignment(
    technician_id: str | None, tech_group_id: str | None, site_id: str | None = None, requester_id: str | None = None
) -> dict:
    return compact(
        {
            "technician": {"userId": text(technician_id)} if text(technician_id) else None,
            "techGroup": {"groupId": text(tech_group_id)} if text(tech_group_id) else None,
            "site": {"id": text(site_id)} if text(site_id) else None,
            "requester": {"userId": text(requester_id)} if text(requester_id) else None,
        }
    )


def ticket_item(ticket_id: str) -> dict:
    return {"workId": ticket_id.strip(), "module": "TICKET"}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def superops_tickets_list(
        status: Annotated[
            list[str] | None, Field(description=f"Any of these statuses. SuperOps ships with: {STOCK_STATUSES}. {TENANT_VALUES}")
        ] = None,
        priority: Annotated[
            list[str] | None, Field(description=f"Any of these priorities. SuperOps ships with: {STOCK_PRIORITIES}. {TENANT_VALUES}")
        ] = None,
        clientId: Annotated[str | None, Field(description="Only this client account id.")] = None,
        technicianId: Annotated[str | None, Field(description="Only tickets assigned to this technician user id.")] = None,
        page: Page = None,
        pageSize: PageSize = None,
        assigneeId: Annotated[str | None, Field(description="Alias of technicianId.")] = None,
        unassigned: Annotated[bool | None, Field(description="Only tickets with no technician assigned.")] = None,
        max: Max = None,
        cursor: Cursor = None,
    ) -> str:
        """List tickets, newest first: subject, status, priority, client, requester and assignee.

        Filters combine with AND; within status or priority any listed value matches.
        """

        async def action(client: SuperOpsClient) -> Any:
            page_no, page_size = paging(page, pageSize, cursor, max)
            technician = alias("technicianId", technicianId, "assigneeId", assigneeId)
            if unassigned and technician:
                raise ValueError("unassigned cannot be combined with technicianId / assigneeId.")
            statuses = [s.strip() for s in status or [] if s and s.strip()]
            priorities = [p.strip() for p in priority or [] if p and p.strip()]
            condition = join(
                "AND",
                [
                    clause("status", "includes", statuses) if statuses else None,
                    clause("priority", "includes", priorities) if priorities else None,
                    clause("client", "includes", [text(clientId)]) if text(clientId) else None,
                    clause("technician", "includes", [technician]) if technician else None,
                    {"attribute": "technician", "operator": "isEmpty"} if unassigned else None,
                ],
            )
            sort = [{"attribute": "createdTime", "order": "DESC"}]
            data = await client.query(TICKET_LIST, {"input": list_input(page_no, page_size, condition, sort)})
            return data.get("getTicketList")

        return await run_tool("superops_tickets_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_tickets_get(ticketId: TicketId) -> str:
        """Get one ticket: classification, SLA times and breaches, worklog total, client, assignee, fields."""

        async def action(client: SuperOpsClient) -> Any:
            data = await client.query(TICKET_GET, {"input": {"ticketId": ticketId.strip()}})
            return data.get("getTicket")

        return await run_tool("superops_tickets_get", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def superops_tickets_create(
        subject: Annotated[str, Field(description="Ticket subject.", min_length=1)],
        clientId: Annotated[str, Field(description="Client account id.", min_length=1)],
        description: Annotated[str | None, Field(description="Problem description.")] = None,
        source: Annotated[Source | None, Field(description="Where the ticket came from (default INTEGRATION).")] = None,
        status: StatusText = None,
        priority: PriorityText = None,
        impact: ImpactText = None,
        urgency: UrgencyText = None,
        category: CategoryText = None,
        subcategory: SubcategoryText = None,
        requestType: RequestTypeText = None,
        siteId: Annotated[str | None, Field(description="Client site id.")] = None,
        requesterId: Annotated[str | None, Field(description="User id of the client contact reporting it.")] = None,
        techGroupId: Annotated[str | None, Field(description="Technician group id to assign.")] = None,
        technicianId: Annotated[str | None, Field(description="Technician user id to assign.")] = None,
        categoryName: Annotated[str | None, Field(description="Alias of category (service category name).")] = None,
        techGroupName: TechGroupName = None,
        requesterEmail: Annotated[
            str | None, Field(description="Email of the client user reporting it; resolved to requesterId within the client.")
        ] = None,
    ) -> str:
        """Create a ticket for a client. Only when the user explicitly asks.

        Status, priority, category and the like must match values configured in the tenant.
        techGroupName and requesterEmail are looked up and must match exactly one group / user.
        """

        async def action(client: SuperOpsClient) -> Any:
            if not text(subject):
                raise ValueError("subject must not be blank.")
            service_category = alias("category", category, "categoryName", categoryName)
            group_id = await resolve_tech_group(client, techGroupId, techGroupName)
            requester_id = await resolve_requester(client, clientId.strip(), requesterId, requesterEmail)
            ticket = compact(
                {
                    "subject": text(subject),
                    "client": {"accountId": clientId.strip()},
                    "source": source or "INTEGRATION",
                    "description": text(description),
                    "status": text(status),
                    "priority": text(priority),
                    "impact": text(impact),
                    "urgency": text(urgency),
                    "category": service_category,
                    "subcategory": text(subcategory),
                    "requestType": text(requestType),
                }
            ) | assignment(technicianId, group_id, siteId, requester_id)
            data = await client.query(TICKET_CREATE, {"input": ticket}, operation="mutation")
            return data.get("createTicket")

        return await run_tool("superops_tickets_create", client_factory, action)

    @mcp.tool(annotations=WRITES_IDEMPOTENT)
    async def superops_tickets_update(
        ticketId: TicketId,
        subject: Annotated[str | None, Field(description="New subject.")] = None,
        status: StatusText = None,
        priority: PriorityText = None,
        impact: ImpactText = None,
        urgency: UrgencyText = None,
        category: CategoryText = None,
        subcategory: SubcategoryText = None,
        requestType: RequestTypeText = None,
        resolutionCode: Annotated[
            str | None, Field(description=f"Resolution code when resolving / closing: {STOCK_RESOLUTION_CODES}. {TENANT_VALUES}")
        ] = None,
        technicianId: Annotated[str | None, Field(description="Reassign to this technician user id.")] = None,
        techGroupId: Annotated[str | None, Field(description="Reassign to this technician group id.")] = None,
        assigneeId: Annotated[str | None, Field(description="Alias of technicianId.")] = None,
        techGroupName: TechGroupName = None,
        resolution: Annotated[
            str | None, Field(description="Resolution notes; added to the ticket as a private note in the same call.")
        ] = None,
    ) -> str:
        """Change a ticket's status, priority, classification or assignee. Only when the user explicitly asks.

        resolutionCode takes a configured code only; resolution text is added as a private note and
        reported under resolutionNote in the result.
        """

        async def action(client: SuperOpsClient) -> Any:
            technician = alias("technicianId", technicianId, "assigneeId", assigneeId)
            group_id = await resolve_tech_group(client, techGroupId, techGroupName)
            note_text = text(resolution)
            changes = compact(
                {
                    "subject": text(subject),
                    "status": text(status),
                    "priority": text(priority),
                    "impact": text(impact),
                    "urgency": text(urgency),
                    "category": text(category),
                    "subcategory": text(subcategory),
                    "requestType": text(requestType),
                    "resolutionCode": text(resolutionCode),
                }
            ) | assignment(technician, group_id)
            if not changes and not note_text:
                raise ValueError("Nothing to update: pass at least one field to change.")
            updated = None
            if changes:
                data = await client.query(TICKET_UPDATE, {"input": {"ticketId": ticketId.strip(), **changes}}, operation="mutation")
                updated = data.get("updateTicket")
            if not note_text:
                return updated
            result: dict[str, Any] = {"ticket": updated}
            note = {"workItem": ticket_item(ticketId), "content": note_text, "privacyType": "PRIVATE"}
            try:
                added = await client.query(NOTE_CREATE, {"input": note}, operation="mutation")
            except SuperOpsError as e:
                if not changes:
                    raise
                # The ticket change already went through; report the failed note instead of hiding it.
                result["resolutionNote"] = None
                result["resolutionNoteError"] = json.loads(e.to_envelope())["error"]
                return result
            result["resolutionNote"] = added.get("createNote")
            return result

        return await run_tool("superops_tickets_update", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def superops_tickets_add_note(
        ticketId: TicketId,
        content: Annotated[str, Field(description="Note text.", min_length=1)],
        isPublic: Annotated[bool | None, Field(description="Visible to the client (default false = private note).")] = None,
    ) -> str:
        """Add a private (default) or client-visible note to a ticket. Only when the user explicitly asks."""

        async def action(client: SuperOpsClient) -> Any:
            if not text(content):
                raise ValueError("content must not be blank.")
            note = {"workItem": ticket_item(ticketId), "content": content, "privacyType": "PUBLIC" if isPublic else "PRIVATE"}
            data = await client.query(NOTE_CREATE, {"input": note}, operation="mutation")
            return data.get("createNote")

        return await run_tool("superops_tickets_add_note", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def superops_tickets_log_time(
        ticketId: TicketId,
        qty: Annotated[
            str | None,
            BeforeValidator(_number_text),
            Field(description='Quantity in the service item\'s unit, usually hours, e.g. "1.5".', pattern=r"^\d+(\.\d+)?$"),
        ] = None,
        billDateTime: Annotated[str | None, Field(description="When the work was done, ISO 8601 (default now).")] = None,
        notes: Annotated[str | None, Field(description="What was done.")] = None,
        billable: Annotated[bool | None, Field(description="Billable (default true).")] = None,
        afterHours: Annotated[bool | None, Field(description="Done after hours (default false).")] = None,
        technicianId: Annotated[str | None, Field(description="Technician who did the work (default: the token's user).")] = None,
        serviceItemId: Annotated[str | None, Field(description="Service catalog item to bill against.")] = None,
        unitPrice: Annotated[str | None, BeforeValidator(_number_text), Field(description="Override the item's unit price.", pattern=r"^\d+(\.\d+)?$")] = None,
        duration: Annotated[float | None, Field(description="Time spent in minutes; converted to hours for qty.", gt=0)] = None,
        description: Annotated[str | None, Field(description="Alias of notes.")] = None,
        workType: Annotated[
            str | None, Field(description='Service catalog item name, e.g. "Remote Support"; resolved to serviceItemId.')
        ] = None,
    ) -> str:
        """Log worked time on a ticket; billable time can end up on a client invoice.

        Pass exactly one of qty (hours) or duration (minutes). Only when the user explicitly asks.
        """

        async def action(client: SuperOpsClient) -> Any:
            if (qty is None) == (duration is None):
                raise ValueError("Pass exactly one of qty (hours) or duration (minutes).")
            quantity = qty if qty is not None else _hours(duration or 0)
            if quantity == "0":
                raise ValueError("duration is too small to log.")
            work_notes = alias("notes", notes, "description", description)
            item_id = await resolve_service_item(client, serviceItemId, workType)
            entry = compact(
                {
                    "workItem": ticket_item(ticketId),
                    "qty": quantity,
                    "billDateTime": text(billDateTime) or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "billable": True if billable is None else billable,
                    "afterHours": bool(afterHours),
                    "notes": work_notes,
                    "technician": {"userId": text(technicianId)} if text(technicianId) else None,
                    "serviceItem": {"itemId": item_id} if item_id else None,
                    "unitPrice": unitPrice,
                }
            )
            data = await client.query(WORKLOG_CREATE, {"input": [entry]}, operation="mutation")
            return data.get("createWorklogEntries")

        return await run_tool("superops_tickets_log_time", client_factory, action)
