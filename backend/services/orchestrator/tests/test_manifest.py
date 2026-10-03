from pathlib import Path

import pytest

from app.core.config import DEFAULT_APPS_DIR
from app.core.manifest import FileManifestStore, ResolutionError
from tests.conftest import APP_ID


def test_real_it_ops_manifest_resolves_entry_agent_and_prompt():
    store = FileManifestStore(DEFAULT_APPS_DIR)
    manifest = store.get("it-ops-triage")
    agent = manifest.agent("")

    assert agent.agent_id == "triage-agent"
    assert agent.role == "entry"
    assert agent.tool_allowlist == ["lookup_runbook"]
    assert "lookup_runbook" in store.prompt(manifest, agent)


def test_manifest_with_wrong_app_id_is_rejected(apps_dir: Path):
    other = apps_dir / "other-app"
    other.mkdir()
    (other / "manifest.yaml").write_text((apps_dir / APP_ID / "manifest.yaml").read_text())
    with pytest.raises(ResolutionError, match="declares app_id"):
        FileManifestStore(apps_dir).get("other-app")


def test_prompt_ref_cannot_escape_the_app_folder(apps_dir: Path):
    store = FileManifestStore(apps_dir)
    manifest = store.get(APP_ID)
    agent = manifest.agent("").model_copy(update={"prompt_ref": "../../etc/passwd"})
    with pytest.raises(ResolutionError, match="prompt_ref"):
        store.prompt(manifest, agent)


def test_unknown_fields_from_later_days_are_ignored(apps_dir: Path):
    path = apps_dir / APP_ID / "manifest.yaml"
    path.write_text(path.read_text() + "\nescalate_when:\n  - field: payload.severity\n    in: [critical]\n")
    assert FileManifestStore(apps_dir).get(APP_ID).app_id == APP_ID
