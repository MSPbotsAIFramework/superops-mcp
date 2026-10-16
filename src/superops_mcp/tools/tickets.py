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

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BeforeValidator, Field

from ..api_client import SuperOpsClient
from ._common import (
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
    Page,
    PageSize,
    clause,
    compact,
    join,
    list_input,
    run_tool,
    text,
)

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


def _number_text(value: Any) -> Any:
    """Accept 1.5 as well as "1.5" for the string-typed quantity fields."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return format(value, "g")
    return value



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
    ) -> str:
        """List tickets, newest first: subject, status, priority, client, requester and assignee.

        Filters combine with AND; within status or priority any listed value matches.
        """

        async def action(client: SuperOpsClient) -> Any:
            statuses = [s.strip() for s in status or [] if s and s.strip()]
            priorities = [p.strip() for p in priority or [] if p and p.strip()]
            condition = join(
                "AND",
                [
                    clause("status", "includes", statuses) if statuses else None,
                    clause("priority", "includes", priorities) if priorities else None,
                    clause("client", "includes", [text(clientId)]) if text(clientId) else None,
                    clause("technician", "includes", [text(technicianId)]) if text(technicianId) else None,
                ],
            )
            sort = [{"attribute": "createdTime", "order": "DESC"}]
            data = await client.query(TICKET_LIST, {"input": list_input(page, pageSize, condition, sort)})
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
    ) -> str:
        """Create a ticket for a client. Only when the user explicitly asks.

        Status, priority, category and the like must match values configured in the tenant.
        """

        async def action(client: SuperOpsClient) -> Any:
            if not text(subject):
                raise ValueError("subject must not be blank.")
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
                    "category": text(category),
                    "subcategory": text(subcategory),
                    "requestType": text(requestType),
                }
            ) | assignment(technicianId, techGroupId, siteId, requesterId)
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
    ) -> str:
        """Change a ticket's status, priority, classification or assignee. Only when the user explicitly asks.

        Write resolution text with superops_tickets_add_note; only a configured resolution code goes here.
        """

        async def action(client: SuperOpsClient) -> Any:
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
            ) | assignment(technicianId, techGroupId)
            if not changes:
                raise ValueError("Nothing to update: pass at least one field to change.")
            data = await client.query(TICKET_UPDATE, {"input": {"ticketId": ticketId.strip(), **changes}}, operation="mutation")
            return data.get("updateTicket")

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
        qty: Annotated[str, BeforeValidator(_number_text), Field(description='Quantity in the service item\'s unit, usually hours, e.g. "1.5".', pattern=r"^\d+(\.\d+)?$")],
        billDateTime: Annotated[str | None, Field(description="When the work was done, ISO 8601 (default now).")] = None,
        notes: Annotated[str | None, Field(description="What was done.")] = None,
        billable: Annotated[bool | None, Field(description="Billable (default true).")] = None,
        afterHours: Annotated[bool | None, Field(description="Done after hours (default false).")] = None,
        technicianId: Annotated[str | None, Field(description="Technician who did the work (default: the token's user).")] = None,
        serviceItemId: Annotated[str | None, Field(description="Service catalog item to bill against.")] = None,
        unitPrice: Annotated[str | None, BeforeValidator(_number_text), Field(description="Override the item's unit price.", pattern=r"^\d+(\.\d+)?$")] = None,
    ) -> str:
        """Log worked time on a ticket; billable time can end up on a client invoice.

        Only when the user explicitly asks.
        """

        async def action(client: SuperOpsClient) -> Any:
            entry = compact(
                {
                    "workItem": ticket_item(ticketId),
                    "qty": qty,
                    "billDateTime": text(billDateTime) or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "billable": True if billable is None else billable,
                    "afterHours": bool(afterHours),
                    "notes": text(notes),
                    "technician": {"userId": text(technicianId)} if text(technicianId) else None,
                    "serviceItem": {"itemId": text(serviceItemId)} if text(serviceItemId) else None,
                    "unitPrice": unitPrice,
                }
            )
            data = await client.query(WORKLOG_CREATE, {"input": [entry]}, operation="mutation")
            return data.get("createWorklogEntries")

        return await run_tool("superops_tickets_log_time", client_factory, action)
