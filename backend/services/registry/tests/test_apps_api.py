"""App Manifest CRUD, validation (every rejection in docs/plan.md Day 5), and resolution."""

import pytest

from tests.conftest import APP_ID, MANIFEST, manifest, tool_body

APP_URL = f"/apps/{APP_ID}"
TOOL_URL = "/tools/lookup_runbook/versions/1.0.0"


def errors(response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    return {e["field"]: e["message"] for e in response.json()["detail"]["errors"]}


# ------------------------------------------------------------------ CRUD


def test_register_then_read_back(registered):
    response = registered.put(APP_URL, json=manifest())
    assert response.status_code == 201
    assert response.json() == {"created": True, "changed": True}

    app = registered.get(APP_URL).json()
    assert app["app_id"] == APP_ID
    assert app["memory_namespace"] == APP_ID
    assert app["alert_key_fields"] == ["alert_type", "host"]
    assert app["escalate_when"] == MANIFEST["escalate_when"]
    assert [a["agent_id"] for a in app["agents"]] == ["triage-agent", "summarizer"]
    assert [a["app_id"] for a in registered.get("/apps").json()] == [APP_ID]


def test_same_manifest_twice_is_a_no_op(registered):
    registered.put(APP_URL, json=manifest())
    response = registered.put(APP_URL, json=manifest())
    assert response.status_code == 200
    assert response.json() == {"created": False, "changed": False}


def test_changed_manifest_is_updated(registered):
    registered.put(APP_URL, json=manifest())
    response = registered.put(APP_URL, json=manifest(display_name="Renamed"))
    assert response.json() == {"created": False, "changed": True}
    assert registered.get(APP_URL).json()["display_name"] == "Renamed"


def test_unknown_app_is_404(client):
    assert client.get("/apps/no-such-app").status_code == 404
    assert client.delete("/apps/no-such-app").status_code == 404


def test_delete_app(registered):
    registered.put(APP_URL, json=manifest())
    assert registered.delete(APP_URL).status_code == 204
    assert registered.get(APP_URL).status_code == 404


def test_agents_are_listed_from_manifests(registered):
    registered.put(APP_URL, json=manifest())
    agents = registered.get("/agents").json()
    assert [(a["app_id"], a["agent_id"], a["role"]) for a in agents] == [
        (APP_ID, "triage-agent", "entry"),
        (APP_ID, "summarizer", "callable"),
    ]


# ------------------------------------------------------------ resolution


def test_agent_tools_are_its_enabled_allowlisted_tools(registered):
    registered.put(APP_URL, json=manifest())
    triage, summarizer = registered.get(APP_URL).json()["agents"]
    assert [t["tool_id"] for t in triage["tools"]] == ["lookup_runbook"]
    assert triage["tools"][0]["input_schema"]["properties"]["alert_type"] == {"type": "string"}
    assert summarizer["tools"] == []


def test_disabling_a_tool_changes_the_next_read_without_re_registering(registered):
    registered.put(APP_URL, json=manifest())
    registered.patch(TOOL_URL, json={"enabled": False})

    app = registered.get(APP_URL).json()
    assert app["agents"][0]["tools"] == []
    assert app["agents"][0]["tool_allowlist"] == ["lookup_runbook"]  # the manifest itself is unchanged
    assert app["tools"] == [{"tool_id": "lookup_runbook", "version": "1.0.0", "scope": "app", "enabled": False}]

    registered.patch(TOOL_URL, json={"enabled": True})
    assert [t["tool_id"] for t in registered.get(APP_URL).json()["agents"][0]["tools"]] == ["lookup_runbook"]


def test_two_apps_get_different_tool_sets(registered):
    registered.put("/tools/billing_lookup/versions/1.0.0", json=tool_body(app_id="other-app"))
    registered.put(APP_URL, json=manifest())
    other = manifest(
        app_id="other-app",
        memory_namespace="other-app",
        tools=[{"tool_id": "billing_lookup", "version": "1.0.0", "scope": "app"}],
    )
    other["agents"] = [{**MANIFEST["agents"][0], "tool_allowlist": ["billing_lookup"]}]
    assert registered.put("/apps/other-app", json=other).status_code == 201

    def tool_ids(app_id):
        return [t["tool_id"] for t in registered.get(f"/apps/{app_id}").json()["agents"][0]["tools"]]

    assert tool_ids(APP_ID) == ["lookup_runbook"]
    assert tool_ids("other-app") == ["billing_lookup"]


# ------------------------------------------------------------ validation


def test_unregistered_tool_is_rejected(client):
    assert "tools[0]" in errors(client.put(APP_URL, json=manifest()))


def test_unregistered_tool_version_is_rejected(registered):
    m = manifest(tools=[{"tool_id": "lookup_runbook", "version": "2.0.0", "scope": "app"}])
    assert "not registered" in errors(registered.put(APP_URL, json=m))["tools[0]"]


def test_scope_mismatch_is_rejected(registered):
    m = manifest(tools=[{"tool_id": "lookup_runbook", "version": "1.0.0", "scope": "global"}])
    assert "tools[0].scope" in errors(registered.put(APP_URL, json=m))


def test_another_apps_tool_is_rejected(registered):
    other = manifest(app_id="other-app", memory_namespace="other-app")
    assert "belongs to app 'test-app'" in errors(registered.put("/apps/other-app", json=other))["tools[0]"]


@pytest.mark.parametrize("roles", [["callable", "callable"], ["entry", "entry"]], ids=["no-entry", "two-entries"])
def test_not_exactly_one_entry_agent_is_rejected(registered, roles):
    m = manifest()
    for agent, role in zip(m["agents"], roles):
        agent["role"] = role
        agent["invoke_on"] = ["ESCALATE"] if role == "callable" else []
    assert "exactly one" in errors(registered.put(APP_URL, json=m))["agents"]


def test_allowlist_entry_not_declared_is_rejected(registered):
    m = manifest()
    m["agents"][0]["tool_allowlist"] = ["lookup_runbook", "billing_lookup"]
    assert "agents[0].tool_allowlist[1]" in errors(registered.put(APP_URL, json=m))


def test_invoke_on_entry_agent_is_rejected(registered):
    m = manifest()
    m["agents"][0]["invoke_on"] = ["ESCALATE"]
    assert "agents[0].invoke_on" in errors(registered.put(APP_URL, json=m))


def test_callable_without_invoke_on_is_rejected(registered):
    m = manifest()
    m["agents"][1]["invoke_on"] = []
    assert "agents[1].invoke_on" in errors(registered.put(APP_URL, json=m))


@pytest.mark.parametrize("value", ["escalate", "DECISION_UNSPECIFIED", "PAGE_SOMEONE"])
def test_invoke_on_must_be_a_decision(registered, value):
    m = manifest()
    m["agents"][1]["invoke_on"] = [value]
    assert "agents[1].invoke_on[0]" in errors(registered.put(APP_URL, json=m))


def test_empty_alert_key_fields_is_rejected(registered):
    assert "alert_key_fields" in errors(registered.put(APP_URL, json=manifest(alert_key_fields=[])))


@pytest.mark.parametrize(
    "field",
    [
        "context.no_such_field", "context.window_24h", "context.window_24h.nope", "severity", "payload.",
        "alert.", "alert.alert_id", "alert.payload", "alert.severity.x", "headers.x",
    ],
)
def test_bad_escalate_when_field_is_rejected(registered, field):
    m = manifest(escalate_when=[{"field": field, "in": [True]}])
    assert "escalate_when[0].field" in errors(registered.put(APP_URL, json=m))


@pytest.mark.parametrize(
    "field",
    [
        "context.is_novel_alert", "context.window_7d.confirmed_incident_count", "payload.labels.team",
        "alert.severity", "alert.source", "alert.message", "alert.alert_key",
    ],
)
def test_valid_escalate_when_fields_are_accepted(registered, field):
    m = manifest(escalate_when=[{"field": field, "in": [True, 3, "x"]}])
    assert registered.put(APP_URL, json=m).status_code == 201


def test_escalate_when_needs_values(registered):
    m = manifest(escalate_when=[{"field": "alert.severity", "in": []}])
    assert registered.put(APP_URL, json=m).status_code == 422


def test_app_id_must_match_url(registered):
    assert "app_id" in errors(registered.put("/apps/other-app", json=manifest()))


def test_unknown_manifest_field_is_rejected(registered):
    assert registered.put(APP_URL, json=manifest(tool_allow_list=[])).status_code == 422


def test_every_problem_is_reported_at_once(registered):
    m = manifest(alert_key_fields=[])
    m["agents"][0]["invoke_on"] = ["ESCALATE"]
    m["agents"][1]["invoke_on"] = []
    assert {"alert_key_fields", "agents[0].invoke_on", "agents[1].invoke_on"} <= errors(
        registered.put(APP_URL, json=m)
    ).keys()


def test_rejected_manifest_is_not_stored(registered):
    registered.put(APP_URL, json=manifest(alert_key_fields=[]))
    assert registered.get(APP_URL).status_code == 404


def test_supervisor_min_confidence_is_stored_and_served(registered):
    assert registered.put(APP_URL, json=manifest(supervisor={"min_confidence": 0.75})).status_code == 201
    assert registered.get(APP_URL).json()["supervisor"] == {"min_confidence": 0.75}


def test_supervisor_is_optional_and_served_as_null(registered):
    m = manifest()
    m.pop("supervisor", None)
    assert registered.put(APP_URL, json=m).status_code == 201
    assert registered.get(APP_URL).json()["supervisor"] is None


@pytest.mark.parametrize("supervisor", [{"min_confidence": 1.5}, {"min_confidence": -0.1}, {"threshold": 0.5}])
def test_bad_supervisor_is_rejected(registered, supervisor):
    assert registered.put(APP_URL, json=manifest(supervisor=supervisor)).status_code == 422
