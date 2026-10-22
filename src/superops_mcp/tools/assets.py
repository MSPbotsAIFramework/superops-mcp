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
"""Assets: managed endpoints, their software and patches."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SuperOpsClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    Cursor,
    Max,
    Offset,
    Page,
    PageSize,
    alias,
    clause,
    join,
    list_input,
    locate,
    page_result,
    paging,
    run_tool,
    text,
)

ASSET_LIST = """
query listAssets($input: ListInfoInput!) {
  getAssetList(input: $input) {
    assets {
      assetId name hostName status platform platformFamily platformVersion publicIp primaryMac
      loggedInUser patchStatus lastCommunicatedTime client site
    }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

ASSET_GET = """
query getAsset($input: AssetIdentifierInput!) {
  getAsset(input: $input) {
    assetId name hostName status lifecycleState platform platformFamily platformCategory platformVersion
    manufacturer model serialNumber primaryMac publicIp gateway domain loggedInUser sysUptime agentVersion
    patchStatus warrantyExpiryDate purchasedDate lastCommunicatedTime lastReportedTime
    client site requester assetClass deviceCategory customFields
  }
}
"""

ASSET_SOFTWARE = """
query assetSoftware($input: AssetDetailsListInput!) {
  getAssetSoftwareList(input: $input) {
    assetSoftwares { id software version installedDate bitVersion installedPath }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

ASSET_PATCHES = """
query assetPatches($input: AssetDetailsListInput!) {
  getAssetPatchDetails(input: $input) {
    assetPatches {
      patchDetail { patchId patchKey title publishedDate category severity kbNumbers { kbNumber } restartRequired }
      approvalStatus installationStatus installationTime failedMessage
    }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

AssetId = Annotated[str, Field(description="Asset id.", min_length=1)]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def superops_assets_list(
        status: Annotated[str | None, Field(description='Agent status, e.g. "ONLINE" or "OFFLINE".')] = None,
        platform: Annotated[str | None, Field(description='Part of the OS name, e.g. "Windows" or "darwin".')] = None,
        clientId: Annotated[str | None, Field(description="Only this client account id.")] = None,
        page: Page = None,
        pageSize: PageSize = None,
        max: Max = None,
        cursor: Cursor = None,
        offset: Offset = None,
    ) -> str:
        """List managed assets by name: host, status, OS, IP, logged-in user, patch status, last contact.

        listInfo.hasMore is null (not false) on the last page.
        """

        async def action(client: SuperOpsClient) -> Any:
            number, size, start = locate(offset, *paging(page, pageSize, cursor, max))
            condition = join(
                "AND",
                [
                    clause("status", "includes", [text(status)]) if text(status) else None,
                    clause("platform", "contains", text(platform)) if text(platform) else None,
                    clause("client", "includes", [text(clientId)]) if text(clientId) else None,
                ],
            )
            sort = [{"attribute": "name", "order": "ASC"}]
            data = await client.query(ASSET_LIST, {"input": list_input(number, size, condition, sort)})
            return page_result(data.get("getAssetList"), "assets", number, size, start)

        return await run_tool("superops_assets_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_assets_get(assetId: AssetId) -> str:
        """Get one asset: hardware (make, model, serial), OS, network, agent version, patch status, warranty.

        CPU / memory / disk figures are not on the asset record; query getAssetSummary or
        getAssetDiskDetails with superops_custom_query.
        """

        async def action(client: SuperOpsClient) -> Any:
            data = await client.query(ASSET_GET, {"input": {"assetId": assetId.strip()}})
            return data.get("getAsset")

        return await run_tool("superops_assets_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_assets_software(
        assetId: AssetId,
        search: Annotated[str | None, Field(description="Part of the software name.")] = None,
        page: Page = None,
        pageSize: PageSize = None,
        max: Max = None,
        offset: Offset = None,
    ) -> str:
        """List software installed on an asset: name, version, install date, bitness and path."""

        async def action(client: SuperOpsClient) -> Any:
            number, size, start = locate(offset, *paging(page, pageSize, None, max))
            condition = clause("software.name", "contains", text(search)) if text(search) else None
            variables = {"input": {"assetId": assetId.strip(), "listInfo": list_input(number, size, condition)}}
            data = await client.query(ASSET_SOFTWARE, variables)
            return page_result(data.get("getAssetSoftwareList"), "assetSoftwares", number, size, start)

        return await run_tool("superops_assets_software", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_assets_patches(
        assetId: AssetId,
        installationStatus: Annotated[str | None, Field(description='Install state, e.g. "Installed" or "NewOrMissing".')] = None,
        severity: Annotated[list[str] | None, Field(description='Any of these severities, e.g. "Recommended", "Others".')] = None,
        page: Page = None,
        pageSize: PageSize = None,
        status: Annotated[str | None, Field(description="Alias of installationStatus.")] = None,
        offset: Offset = None,
    ) -> str:
        """List an asset's patches: title, KB numbers, category, severity, approval and install status."""

        async def action(client: SuperOpsClient) -> Any:
            severities = [s.strip() for s in severity or [] if s and s.strip()]
            install_status = alias("installationStatus", installationStatus, "status", status)
            condition = join(
                "AND",
                [
                    clause("installationStatus", "includes", [install_status]) if install_status else None,
                    clause("severity", "includes", severities) if severities else None,
                ],
            )
            number, size, start = locate(offset, page, pageSize)
            variables = {"input": {"assetId": assetId.strip(), "listInfo": list_input(number, size, condition)}}
            data = await client.query(ASSET_PATCHES, variables)
            return page_result(data.get("getAssetPatchDetails"), "assetPatches", number, size, start)

        return await run_tool("superops_assets_patches", client_factory, action)
