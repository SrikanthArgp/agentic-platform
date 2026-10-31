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
    assert app["tools"] == [
        {"tool_id": "lookup_runbook", "version": "1.0.0", "requested_version": "1.0.0", "scope": "app", "enabled": False}
    ]

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
    assert registered.get(APP_URL).json()["supervisor"] == {
        "min_confidence": 0.75, "suppress_requires_noise_verdict": False,
    }


def test_supervisor_suppress_requires_noise_verdict_is_stored_and_served(registered):
    m = manifest(supervisor={"min_confidence": 0.6, "suppress_requires_noise_verdict": True})
    assert registered.put(APP_URL, json=m).status_code == 201
    assert registered.get(APP_URL).json()["supervisor"]["suppress_requires_noise_verdict"] is True


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_supervisor_suppress_requires_noise_verdict_must_be_a_boolean(registered, value):
    m = manifest(supervisor={"suppress_requires_noise_verdict": value})
    assert registered.put(APP_URL, json=m).status_code == 422


def test_supervisor_is_optional_and_served_as_null(registered):
    m = manifest()
    m.pop("supervisor", None)
    assert registered.put(APP_URL, json=m).status_code == 201
    assert registered.get(APP_URL).json()["supervisor"] is None


@pytest.mark.parametrize("supervisor", [{"min_confidence": 1.5}, {"min_confidence": -0.1}, {"threshold": 0.5}])
def test_bad_supervisor_is_rejected(registered, supervisor):
    assert registered.put(APP_URL, json=manifest(supervisor=supervisor)).status_code == 422


# --- version resolution: pinned or "latest" (ADR-0026) ---

LATEST = [{"tool_id": "lookup_runbook", "version": "latest", "scope": "app"}]


def _served_versions(client) -> tuple[str | None, list[str]]:
    app = client.get(APP_URL).json()
    return app["tools"][0]["version"], [t["version"] for t in app["agents"][0]["tools"]]


def test_latest_resolves_to_the_highest_registered_version_by_number(registered):
    for version in ("1.9.0", "1.10.0", "1.2.3"):
        registered.put(f"/tools/lookup_runbook/versions/{version}", json=tool_body())
    assert registered.put(APP_URL, json=manifest(tools=LATEST)).status_code == 201

    app = registered.get(APP_URL).json()
    assert app["tools"][0] == {
        "tool_id": "lookup_runbook", "version": "1.10.0", "requested_version": "latest", "scope": "app", "enabled": True,
    }
    assert app["agents"][0]["tools"][0]["version"] == "1.10.0"


def test_a_new_version_moves_latest_without_re_registering_the_app(registered):
    registered.put(APP_URL, json=manifest(tools=LATEST))
    assert _served_versions(registered) == ("1.0.0", ["1.0.0"])

    registered.put("/tools/lookup_runbook/versions/1.1.0", json=tool_body(description="v1.1"))
    assert _served_versions(registered) == ("1.1.0", ["1.1.0"])
    assert registered.get(APP_URL).json()["agents"][0]["tools"][0]["description"] == "v1.1"


def test_pinned_version_stays_put_when_a_newer_one_is_registered(registered):
    registered.put(APP_URL, json=manifest())
    registered.put("/tools/lookup_runbook/versions/2.0.0", json=tool_body())

    assert _served_versions(registered) == ("1.0.0", ["1.0.0"])


def test_disabled_latest_turns_the_tool_off_instead_of_falling_back(registered):
    registered.put("/tools/lookup_runbook/versions/1.1.0", json=tool_body())
    registered.put(APP_URL, json=manifest(tools=LATEST))
    registered.patch("/tools/lookup_runbook/versions/1.1.0", json={"enabled": False})

    app = registered.get(APP_URL).json()
    assert app["tools"][0]["version"] == "1.1.0"
    assert app["tools"][0]["enabled"] is False
    assert app["agents"][0]["tools"] == []


def test_latest_with_no_registered_version_is_rejected(client):
    response = client.put(APP_URL, json=manifest(tools=LATEST))

    assert response.status_code == 422
    assert "has no registered version" in str(response.json())


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "Latest", "newest", "1.0.0-beta"])
def test_malformed_version_reference_is_rejected(registered, version):
    tools = [{"tool_id": "lookup_runbook", "version": version, "scope": "app"}]
    assert registered.put(APP_URL, json=manifest(tools=tools)).status_code == 422


def test_deleting_the_newest_version_moves_latest_back_one(registered):
    """How a global tool's upgrade is rolled back."""
    registered.put("/tools/lookup_runbook/versions/1.1.0", json=tool_body())
    registered.put(APP_URL, json=manifest(tools=LATEST))

    assert registered.delete("/tools/lookup_runbook/versions/1.1.0").status_code == 204
    assert _served_versions(registered) == ("1.0.0", ["1.0.0"])


def test_deleting_the_only_version_latest_has_is_refused(registered):
    registered.put(APP_URL, json=manifest(tools=LATEST))

    assert registered.delete(TOOL_URL).status_code == 409
    assert _served_versions(registered) == ("1.0.0", ["1.0.0"])


def test_deleting_a_pinned_version_is_refused_even_with_newer_ones(registered):
    registered.put("/tools/lookup_runbook/versions/1.1.0", json=tool_body())
    registered.put(APP_URL, json=manifest())

    assert registered.delete(TOOL_URL).status_code == 409
    assert registered.delete("/tools/lookup_runbook/versions/1.1.0").status_code == 204


# --- prefetch_tools (ADR-0028) ---


def _with_prefetch(prefetch: list[str], allowlist: list[str] | None = None) -> dict:
    m = manifest()
    m["agents"][0]["prefetch_tools"] = prefetch
    if allowlist is not None:
        m["agents"][0]["tool_allowlist"] = allowlist
    return m


def test_prefetch_tools_are_stored_and_served(registered):
    assert registered.put(APP_URL, json=_with_prefetch(["lookup_runbook"])).status_code == 201

    agents = registered.get(APP_URL).json()["agents"]
    assert agents[0]["prefetch_tools"] == ["lookup_runbook"]
    assert agents[1]["prefetch_tools"] == []


def test_prefetch_tool_not_on_the_allowlist_is_rejected(registered):
    response = registered.put(APP_URL, json=_with_prefetch(["lookup_runbook"], allowlist=[]))

    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert {"field": "agents[0].prefetch_tools[0]", "message": "'lookup_runbook' is not in this agent's tool_allowlist."} in errors


def test_prefetch_tool_that_needs_arguments_is_rejected(client):
    schema = {"type": "object", "properties": {"alert_type": {"type": "string"}}, "required": ["alert_type"]}
    client.put("/tools/lookup_runbook/versions/1.0.0", json=tool_body(input_schema=schema))
    response = client.put(APP_URL, json=_with_prefetch(["lookup_runbook"]))

    assert response.status_code == 422
    assert "can't be called with no arguments" in str(response.json())


def test_prefetch_tools_must_not_repeat(registered):
    assert registered.put(APP_URL, json=_with_prefetch(["lookup_runbook", "lookup_runbook"])).status_code == 422


# --- budgets and rate limits (ADR-0029) ---


def test_budget_and_rate_limit_are_stored_and_served(registered):
    m = manifest(rate_limit={"per_minute": 600, "per_source_per_minute": 120})
    m["agents"][0]["budget"] = {"daily_tokens": 50000}
    assert registered.put(APP_URL, json=m).status_code == 201

    app = registered.get(APP_URL).json()
    assert app["rate_limit"] == {"per_minute": 600, "per_source_per_minute": 120}
    assert app["agents"][0]["budget"] == {"daily_tokens": 50000}
    assert app["agents"][1]["budget"] is None


def test_budget_and_rate_limit_are_optional(registered):
    registered.put(APP_URL, json=manifest())
    app = registered.get(APP_URL).json()
    assert app["rate_limit"] is None
    assert all(a["budget"] is None for a in app["agents"])


@pytest.mark.parametrize(
    "rate_limit",
    [{"per_minute": 0}, {"per_source_per_minute": -1}, {"per_minute": "lots"}, {"per_hour": 10}],
)
def test_bad_rate_limit_is_rejected(registered, rate_limit):
    assert registered.put(APP_URL, json=manifest(rate_limit=rate_limit)).status_code == 422


@pytest.mark.parametrize("budget", [{"daily_tokens": -1}, {"daily_tokens": 1.5}, {"tokens": 10}])
def test_bad_budget_is_rejected(registered, budget):
    m = manifest()
    m["agents"][0]["budget"] = budget
    assert registered.put(APP_URL, json=m).status_code == 422
