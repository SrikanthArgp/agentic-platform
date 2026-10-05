"""A stored manifest + current tool rows -> what `GET /apps/{app_id}` serves.

An agent's effective tools are its `tool_allowlist`, keeping only tools the
manifest declares, that are registered, and that are enabled right now.
Computed here so `orchestrator` (which tools to offer) and `tool-gateway`
(which calls to allow) read the same answer (docs/ARCHITECTURE.md §3).
"""

from collections.abc import Mapping

from app.core.models import AppRecord, EffectiveTool, ResolvedAgent, ResolvedApp, ResolvedToolRef, Tool


def resolve_app(record: AppRecord, tools: Mapping[tuple[str, str], Tool]) -> ResolvedApp:
    """`tools` maps (tool_id, version) to the registered tool, for the
    manifest's declared tools. A declared tool missing from it (deleted since
    registration) counts as disabled."""
    m = record.manifest
    declared = {t["tool_id"]: (t["tool_id"], t["version"]) for t in m["tools"]}

    def enabled(tool_id: str) -> Tool | None:
        key = declared.get(tool_id)
        tool = tools.get(key) if key else None
        return tool if tool is not None and tool.enabled else None

    agents = []
    for a in m["agents"]:
        effective = [
            EffectiveTool(
                tool_id=t.tool_id, version=t.version, scope=t.scope,
                description=t.description, input_schema=t.input_schema,
            )
            for tool_id in a["tool_allowlist"]
            if (t := enabled(tool_id)) is not None
        ]
        agents.append(ResolvedAgent(**a, tools=effective))

    return ResolvedApp(
        app_id=record.app_id,
        display_name=record.display_name,
        agents=agents,
        tools=[
            ResolvedToolRef(**t, enabled=enabled(t["tool_id"]) is not None)
            for t in m["tools"]
        ],
        event_schema_ref=m["event_schema_ref"],
        alert_key_fields=m["alert_key_fields"],
        memory_namespace=m["memory_namespace"],
        escalate_when=m["escalate_when"],
        # Manifests stored before Day 7 have no supervisor key.
        supervisor=m.get("supervisor"),
        updated_at=record.updated_at,
    )
