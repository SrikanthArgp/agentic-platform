import pytest

from app.core.config import DEFAULT_APPS_DIR
from app.core.manifest import ManifestStore, ManifestUnavailableError, ResolutionError
from registry_client import RegistryUnavailableError
from tests.conftest import APP_ID, FakeRegistry

pytestmark = pytest.mark.anyio


async def test_resolves_entry_agent_tools_and_prompt(apps_dir):
    store = ManifestStore(FakeRegistry(), apps_dir)
    manifest = await store.get(APP_ID)
    agent = manifest.agent("")

    assert agent.agent_id == "triage-agent"
    assert [t.tool_id for t in agent.tools] == ["lookup_runbook"]
    assert manifest.memory_namespace == APP_ID
    assert "lookup_runbook" in store.prompt(manifest, agent)


async def test_real_it_ops_prompt_is_in_the_image():
    app = {"app_id": "it-ops-triage", "display_name": "x", "agents": [
        {"agent_id": "triage-agent", "version": "0.1.0", "role": "entry", "prompt_ref": "prompts/triage-agent.md"}
    ]}
    store = ManifestStore(FakeRegistry({"it-ops-triage": app}), DEFAULT_APPS_DIR)
    manifest = await store.get("it-ops-triage")
    assert "lookup_runbook" in store.prompt(manifest, manifest.agent(""))


async def test_unknown_app_is_a_resolution_error(apps_dir):
    with pytest.raises(ResolutionError, match="no-such-app"):
        await ManifestStore(FakeRegistry(), apps_dir).get("no-such-app")


async def test_registry_down_is_not_a_resolution_error(apps_dir):
    class Down:
        async def get_app(self, app_id):
            raise RegistryUnavailableError("connection refused")

    with pytest.raises(ManifestUnavailableError, match="registry unavailable"):
        await ManifestStore(Down(), apps_dir).get(APP_ID)


async def test_mismatched_app_id_is_rejected(apps_dir):
    registry = FakeRegistry()
    registry.apps["other-app"] = registry.apps[APP_ID]
    with pytest.raises(ResolutionError, match="returned app_id"):
        await ManifestStore(registry, apps_dir).get("other-app")


async def test_prompt_ref_cannot_escape_the_app_folder(apps_dir):
    store = ManifestStore(FakeRegistry(), apps_dir)
    manifest = await store.get(APP_ID)
    agent = manifest.agent("").model_copy(update={"prompt_ref": "../../etc/passwd"})
    with pytest.raises(ResolutionError, match="prompt_ref"):
        store.prompt(manifest, agent)


async def test_fields_added_by_registry_later_are_ignored(apps_dir):
    registry = FakeRegistry()
    registry.apps[APP_ID]["some_future_field"] = {"x": 1}
    assert (await ManifestStore(registry, apps_dir).get(APP_ID)).app_id == APP_ID


async def test_escalate_when_and_supervisor_parse_from_registry(apps_dir):
    registry = FakeRegistry()
    registry.apps[APP_ID]["escalate_when"] = [
        {"field": "alert.severity", "in": ["critical"]},
        {"field": "context.has_confirmed_incident_history", "in": [True]},
    ]
    registry.apps[APP_ID]["supervisor"] = {"min_confidence": 0.8}
    manifest = await ManifestStore(registry, apps_dir).get(APP_ID)

    assert [(r.field, list(r.values)) for r in manifest.guardrails()] == [
        ("alert.severity", ["critical"]),
        ("context.has_confirmed_incident_history", [True]),
    ]
    assert manifest.min_confidence(default=0.6) == 0.8


@pytest.mark.parametrize("supervisor", [None, {}, {"min_confidence": None}])
async def test_min_confidence_falls_back_to_the_platform_default(apps_dir, supervisor):
    registry = FakeRegistry()
    registry.apps[APP_ID]["supervisor"] = supervisor
    manifest = await ManifestStore(registry, apps_dir).get(APP_ID)
    assert manifest.min_confidence(default=0.6) == 0.6
