# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "pyyaml>=6", "mcp>=2.2,<3"]
# ///
"""Register an app in `registry` from its checked-in manifest (ADR-0006, docs/ARCHITECTURE.md §12).

    uv run backend/scripts/register_app.py it-ops-triage
    uv run backend/scripts/register_app.py it-ops-triage --registry-url http://localhost:8005 \\
        --tool-gateway-url http://localhost:8003/mcp

1. Reads `backend/apps/{app_id}/manifest.yaml`.
2. Registers each tool the manifest declares, with the definition the
   running `tool-gateway` serves (description, schemas, `read_only`). A
   declared tool `tool-gateway` doesn't serve, at that version, fails the
   run: app code ships first, the manifest second (§12).
3. Upserts the manifest. `registry` validates it in full and rejects it
   with every problem listed.

Idempotent: re-running with nothing changed reports "unchanged" for every
step and changes nothing. Exits 1 on any failure.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

APPS_DIR = Path(__file__).resolve().parents[1] / "apps"
DEFAULT_REGISTRY_URL = "http://localhost:8005"
DEFAULT_TOOL_GATEWAY_URL = "http://localhost:8003/mcp"


class RegistrationError(RuntimeError):
    pass


def load_manifest(app_id: str, apps_dir: Path = APPS_DIR) -> dict[str, Any]:
    path = apps_dir / app_id / "manifest.yaml"
    if not path.is_file():
        raise RegistrationError(f"{path} does not exist.")
    manifest = yaml.safe_load(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("app_id") != app_id:
        raise RegistrationError(f"{path} must be a mapping with app_id: {app_id}.")
    return manifest


async def served_tools(tool_gateway_url: str) -> dict[str, dict[str, Any]]:
    """tool_id -> definition, as `tool-gateway`'s MCP `tools/list` reports it."""
    from mcp import Client

    async with Client(tool_gateway_url) as client:
        tools = (await client.list_tools()).tools
    return {
        t.name: {
            "description": t.description or "",
            "input_schema": t.input_schema,
            "output_schema": t.output_schema,
            **(t.meta or {}),
        }
        for t in tools
    }


def register(manifest: dict[str, Any], served: dict[str, dict[str, Any]], http: httpx.Client) -> list[str]:
    """Register the manifest's tools, then the manifest. Returns one line per step."""
    app_id = manifest["app_id"]
    report = []
    for ref in manifest.get("tools", []):
        tool_id, version = ref["tool_id"], ref["version"]
        tool = served.get(tool_id)
        if tool is None or tool.get("version") != version:
            have = f"version {tool.get('version')}" if tool else "nothing"
            raise RegistrationError(
                f"tool-gateway serves {have} for '{tool_id}', the manifest declares {version}. "
                "Roll out the app's code before registering its manifest."
            )
        body = {
            "description": tool["description"],
            "scope": tool.get("scope"),
            "app_id": tool.get("app_id"),
            "input_schema": tool["input_schema"],
            "output_schema": tool.get("output_schema"),
            "read_only": tool.get("read_only", False),
        }
        response = http.put(f"/tools/{tool_id}/versions/{version}", json=body)
        report.append(f"tool {tool_id} {version}: {_outcome(response)}")

    response = http.put(f"/apps/{app_id}", json=manifest)
    report.append(f"app {app_id}: {_outcome(response)}")
    return report


def _outcome(response: httpx.Response) -> str:
    if response.status_code in (200, 201):
        result = response.json()
        return "created" if result["created"] else "updated" if result["changed"] else "unchanged"
    detail = response.json().get("detail", response.text) if _is_json(response) else response.text
    if isinstance(detail, dict) and "errors" in detail:
        lines = "\n".join(f"  {e['field']}: {e['message']}" for e in detail["errors"])
        detail = f"{detail.get('message', '')}\n{lines}"
    raise RegistrationError(f"{response.request.method} {response.request.url.path} -> {response.status_code}: {detail}")


def _is_json(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("app_id")
    parser.add_argument("--registry-url", default=DEFAULT_REGISTRY_URL)
    parser.add_argument("--tool-gateway-url", default=DEFAULT_TOOL_GATEWAY_URL)
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.app_id)
        served = asyncio.run(served_tools(args.tool_gateway_url))
        with httpx.Client(base_url=args.registry_url, timeout=10) as http:
            for line in register(manifest, served, http):
                print(line)
    except (RegistrationError, httpx.HTTPError) as e:
        print(f"registration failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
