import pytest

from run_context import APP_ID_KEY, AGENT_ID_KEY, RunContext, from_meta


def test_meta_round_trip():
    ctx = RunContext(app_id="it-ops-triage", agent_id="triage-agent", alert_id="a-1")
    assert from_meta(ctx.to_meta()) == ctx


@pytest.mark.parametrize(
    "meta",
    [None, {}, {APP_ID_KEY: "it-ops-triage"}, {APP_ID_KEY: "", AGENT_ID_KEY: "x"}, {APP_ID_KEY: 1, AGENT_ID_KEY: "x"}],
)
def test_missing_or_malformed_context_is_none(meta):
    assert from_meta(meta) is None


def test_alert_id_is_optional_and_other_meta_keys_are_ignored():
    ctx = from_meta({APP_ID_KEY: "app", AGENT_ID_KEY: "agent", "progressToken": 7})
    assert ctx == RunContext(app_id="app", agent_id="agent", alert_id="")
