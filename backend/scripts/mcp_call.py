# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp>=2.2,<3"]
# ///
"""Standalone MCP client for tool-gateway: list its tools, or call one.

    uv run backend/scripts/mcp_call.py                      # list tools
    uv run backend/scripts/mcp_call.py lookup_runbook '{"alert_type": "disk_full"}'
    uv run backend/scripts/mcp_call.py --url http://localhost:8003/mcp lookup_runbook '{"alert_type": "nope"}'
    uv run backend/scripts/mcp_call.py --app-id cost-anomaly-triage --agent-id cost-triage-agent billing_lookup '{...}'

Calls run as an agent: tool-gateway only serves a call carrying a run
context (`--app-id`/`--agent-id`, sent in `_meta` like orchestrator does)
whose agent `registry` allows that tool, so the app must be registered.

Exits 1 when the tool returns an error result (tool_not_allowed,
registry_unavailable, tool_not_found, invalid_arguments, tool_failed). A runbook that isn't found is a normal
result (found=false), not an error.
"""

import argparse
import asyncio
import json
import sys

from mcp import Client

DEFAULT_URL = "http://localhost:8003/mcp"
# The run_context keys (backend/shared/run_context), without installing ap-shared.
META_PREFIX = "agentic-platform/"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--app-id", default="it-ops-triage")
    parser.add_argument("--agent-id", default="triage-agent")
    parser.add_argument("tool", nargs="?", help="tool_id to call; omit to list tools")
    parser.add_argument("arguments", nargs="?", default="{}", help="tool arguments as a JSON object")
    args = parser.parse_args()

    async with Client(args.url) as client:
        if args.tool is None:
            for tool in (await client.list_tools()).tools:
                print(f"{tool.name}  {json.dumps(tool.meta)}\n  {tool.description}")
                print(f"  input: {json.dumps(tool.input_schema)}")
            return 0

        meta = {META_PREFIX + "app_id": args.app_id, META_PREFIX + "agent_id": args.agent_id,
                META_PREFIX + "alert_id": "mcp_call"}
        result = await client.call_tool(args.tool, json.loads(args.arguments), meta=meta)
        print(json.dumps(result.structured_content, indent=2))
        return 1 if result.is_error else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
