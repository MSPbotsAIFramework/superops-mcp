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
"""Tool-contract compatibility with the deployed (int_baseline) and upstream tool lists.

Every tool in either list must exist, accept every parameter of both lists, and
never require a parameter the deployed contract did not require.
"""

import asyncio
import json
from pathlib import Path

import pytest

from superops_mcp.server import create_mcp_server

BASELINE = json.loads((Path(__file__).parent / "contract_baseline.json").read_text(encoding="utf-8"))
INT_BASELINE: dict = BASELINE["int_baseline"]
UPSTREAM: dict = BASELINE["upstream"]
ALL_TOOLS = sorted(set(INT_BASELINE) | set(UPSTREAM))


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_tool_exists(tools, name):
    assert name in tools


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_params_are_a_superset(tools, name):
    ours = set(tools[name].inputSchema.get("properties", {}))
    wanted = set(INT_BASELINE.get(name, {}).get("params", [])) | set(UPSTREAM.get(name, {}).get("params", []))
    assert wanted <= ours, f"{name} is missing {sorted(wanted - ours)}"


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_required_is_a_subset_of_int_baseline(tools, name):
    ours = set(tools[name].inputSchema.get("required", []))
    # A tool the deployed contract never had falls back to the upstream required set.
    allowed = set((INT_BASELINE.get(name) or UPSTREAM[name]).get("required", []))
    assert ours <= allowed, f"{name} newly requires {sorted(ours - allowed)}"
