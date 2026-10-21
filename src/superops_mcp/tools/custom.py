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
"""Raw GraphQL for anything the other tools do not cover."""

from typing import Annotated, Any

from graphql import GraphQLSyntaxError, OperationDefinitionNode, OperationType, parse
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SuperOpsClient
from ._common import ANY_WRITE, READ_ONLY, ClientFactory, run_tool

Variables = Annotated[dict[str, Any] | None, Field(description="GraphQL variables.")]


def operation_types(document: str) -> set[OperationType]:
    """The operation kinds in a GraphQL document; raises ValueError when it does not parse."""
    try:
        parsed = parse(document)
    except GraphQLSyntaxError as e:
        raise ValueError(f"Not valid GraphQL: {e.message} (line {e.locations[0].line if e.locations else '?'})") from e
    kinds = {d.operation for d in parsed.definitions if isinstance(d, OperationDefinitionNode)}
    if not kinds:
        raise ValueError("The document contains no operation.")
    return kinds


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def superops_custom_query(
        query: Annotated[str | None, Field(description="GraphQL query document (query operations only). Required.")] = None,
        variables: Variables = None,
    ) -> str:
        """Run a read-only GraphQL query against the SuperOps API, e.g. getAssetSummary or getTicketNoteList.

        Mutations are refused here; use superops_custom_mutation for writes.
        """

        async def action(client: SuperOpsClient) -> Any:
            if not (query or "").strip():
                raise ValueError("query is required: pass a GraphQL query document.")
            kinds = operation_types(query)
            if kinds != {OperationType.QUERY}:
                raise ValueError("superops_custom_query only runs query operations; use superops_custom_mutation for writes.")
            return await client.query(query, variables)

        return await run_tool("superops_custom_query", client_factory, action)

    @mcp.tool(annotations=ANY_WRITE)
    async def superops_custom_mutation(
        mutation: Annotated[str, Field(description="GraphQL mutation document.", min_length=1)],
        variables: Variables = None,
    ) -> str:
        """Run a GraphQL mutation for a write no other tool covers. Can change or delete data.

        Only when the user explicitly asks for that exact change. Never retried automatically.
        """

        async def action(client: SuperOpsClient) -> Any:
            kinds = operation_types(mutation)
            if OperationType.MUTATION not in kinds or OperationType.SUBSCRIPTION in kinds:
                raise ValueError("superops_custom_mutation needs a mutation operation; use superops_custom_query for reads.")
            return await client.query(mutation, variables, operation="mutation")

        return await run_tool("superops_custom_mutation", client_factory, action)
