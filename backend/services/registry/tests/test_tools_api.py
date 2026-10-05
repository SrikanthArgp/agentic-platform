"""Tool registration CRUD, against the in-memory repository."""

import pytest

from tests.conftest import APP_ID, manifest, tool_body

URL = "/tools/lookup_runbook/versions/1.0.0"


def test_register_then_read_back(client):
    response = client.put(URL, json=tool_body())
    assert response.status_code == 201
    assert response.json() == {"created": True, "changed": True}

    tool = client.get(URL).json()
    assert (tool["tool_id"], tool["version"], tool["app_id"]) == ("lookup_runbook", "1.0.0", APP_ID)
    assert tool["enabled"] is True and tool["read_only"] is True
    assert [t["tool_id"] for t in client.get("/tools").json()] == ["lookup_runbook"]


def test_same_registration_twice_is_a_no_op(client):
    client.put(URL, json=tool_body())
    response = client.put(URL, json=tool_body())
    assert response.status_code == 200
    assert response.json() == {"created": False, "changed": False}


def test_changed_definition_is_updated(client):
    client.put(URL, json=tool_body())
    response = client.put(URL, json=tool_body(description="New text."))
    assert response.json() == {"created": False, "changed": True}
    assert client.get(URL).json()["description"] == "New text."


def test_reregistering_does_not_re_enable_a_disabled_tool(client):
    client.put(URL, json=tool_body())
    client.patch(URL, json={"enabled": False})
    client.put(URL, json=tool_body(description="New text."))
    assert client.get(URL).json()["enabled"] is False


def test_tool_without_read_only_true_is_rejected(client):
    response = client.put(URL, json=tool_body(read_only=False))
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["field"] == "read_only"
    assert client.get(URL).status_code == 404


def test_tool_missing_read_only_is_rejected(client):
    body = tool_body()
    del body["read_only"]
    assert client.put(URL, json=body).status_code == 422


@pytest.mark.parametrize(
    "body",
    [tool_body(app_id=None, scope="app"), tool_body(scope="global")],
    ids=["app-scope-without-app_id", "global-with-app_id"],
)
def test_scope_and_app_id_must_agree(client, body):
    response = client.put(URL, json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["field"] == "app_id"


@pytest.mark.parametrize("url", ["/tools/Bad Name/versions/1.0.0", "/tools/lookup_runbook/versions/latest"])
def test_bad_tool_id_or_version_is_rejected(client, url):
    assert client.put(url, json=tool_body()).status_code == 422


def test_unknown_tool_is_404(client):
    assert client.get(URL).status_code == 404
    assert client.patch(URL, json={"enabled": False}).status_code == 404
    assert client.delete(URL).status_code == 404


def test_disable_and_re_enable(client):
    client.put(URL, json=tool_body())
    assert client.patch(URL, json={"enabled": False}).json()["enabled"] is False
    assert client.patch(URL, json={"enabled": True}).json()["enabled"] is True


def test_delete_unused_tool(client):
    client.put(URL, json=tool_body())
    assert client.delete(URL).status_code == 204
    assert client.get(URL).status_code == 404


def test_delete_refuses_a_tool_a_manifest_declares(registered):
    registered.put(f"/apps/{APP_ID}", json=manifest())
    response = registered.delete(URL)
    assert response.status_code == 409
    assert APP_ID in response.json()["detail"]
