"""App-owned tools of cost-anomaly-triage, loaded from the real backend/apps
the same way tool-gateway loads them at startup."""

import pytest
from pydantic import ValidationError

from app.core.loader import load_app_tools
from app.core.registry import ToolRegistry
from tests.conftest import REPO_APPS_DIR

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def billing_lookup():
    registry = ToolRegistry()
    load_app_tools(REPO_APPS_DIR, registry)
    return registry.get("billing-lookup")


async def lookup(tool, service: str, region: str, start: str, end: str) -> dict:
    out = await tool.invoke({"service": service, "region": region, "window_start": start, "window_end": end})
    return out.model_dump(mode="json")


def test_registered_as_cost_anomaly_triage_app_tool(billing_lookup):
    assert billing_lookup.scope == "app"
    assert billing_lookup.app_id == "cost-anomaly-triage"
    assert billing_lookup.version == "1.0.0"


async def test_monthly_batch_day_shows_the_tagged_emr_cluster(billing_lookup):
    out = await lookup(billing_lookup, "checkout-svc", "us-east-1", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")

    assert out["found"] is True
    assert out["total_cost_usd"] == pytest.approx(3 * 80.40 + 151.20 + 28.75 + 1146.90)
    top = out["by_resource"][0]
    assert top["resource_id"] == "emr-recon-202610"
    assert top["tags"]["schedule"] == "monthly"
    assert {"key": "job", "value": "monthly-reconciliation", "cost_usd": 1146.90} in out["by_tag"]
    assert out["untagged_cost_usd"] == 0


async def test_novel_key_has_the_same_shape_of_tagged_monthly_batch(billing_lookup):
    """Day 17's contrast for checkout-svc: same tags claiming a monthly job,
    but no past cases on reporting-svc:us-east-2 to confirm them."""
    out = await lookup(billing_lookup, "reporting-svc", "us-east-2", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")

    assert out["total_cost_usd"] == pytest.approx(40.20 + 62.40 + 884.30)
    top = out["by_resource"][0]
    assert (top["resource_id"], top["tags"]["schedule"]) == ("emr-export-202610", "monthly")
    assert out["untagged_cost_usd"] == 0
    normal = await lookup(billing_lookup, "reporting-svc", "us-east-2", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z")
    assert normal["total_cost_usd"] == pytest.approx(40.20 + 62.40)


async def test_runaway_gpu_spend_is_untagged(billing_lookup):
    out = await lookup(billing_lookup, "ml-platform", "eu-west-1", "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z")

    assert out["total_cost_usd"] == pytest.approx(8 * 786.43 + 242.10 + 61.30)
    assert out["untagged_cost_usd"] == pytest.approx(8 * 786.43)
    assert [r["resource_type"] for r in out["by_resource"][:8]] == ["ec2:p4d.24xlarge"] * 8
    assert all(r["tags"] == {} for r in out["by_resource"][:8])


async def test_window_counts_whole_days_and_the_end_is_exclusive(billing_lookup):
    one_day = await lookup(billing_lookup, "search-api", "us-west-2", "2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z")
    two_days = await lookup(billing_lookup, "search-api", "us-west-2", "2026-10-06T00:00:00Z", "2026-10-08T00:00:00Z")

    assert one_day["total_cost_usd"] == pytest.approx(2 * 81.60 + 39.50)
    assert two_days["total_cost_usd"] == pytest.approx(one_day["total_cost_usd"] * 2 + 2 * 81.60)
    assert {"key": "event", "value": "product-launch", "cost_usd": 163.2} in two_days["by_tag"]


@pytest.mark.parametrize(
    "service, region, start",
    [
        ("checkout-svc", "eu-west-1", "2026-10-01T00:00:00Z"),  # service exists, not in that region
        ("no-such-svc", "us-east-1", "2026-10-01T00:00:00Z"),
        ("checkout-svc", "us-east-1", "2027-01-01T00:00:00Z"),  # outside the fixture's dates
    ],
)
async def test_no_data_is_found_false_not_an_error(billing_lookup, service, region, start):
    end = start.replace("T00", "T23")
    out = await lookup(billing_lookup, service, region, start, end)

    assert out["found"] is False
    assert out["total_cost_usd"] == 0
    assert out["message"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"service": "Checkout", "region": "us-east-1", "window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-02T00:00:00Z"},
        {"service": "checkout-svc", "region": "useast1", "window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-02T00:00:00Z"},
        {"service": "checkout-svc", "region": "us-east-1", "window_start": "2026-10-01T00:00:00", "window_end": "2026-10-02T00:00:00Z"},
        {"service": "checkout-svc", "region": "us-east-1", "window_start": "2026-10-02T00:00:00Z", "window_end": "2026-10-01T00:00:00Z"},
        {"service": "checkout-svc", "region": "us-east-1", "window_start": "2026-08-01T00:00:00Z", "window_end": "2026-10-01T00:00:00Z"},
        {"service": "checkout-svc", "region": "us-east-1", "window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-02T00:00:00Z", "app_id": "it-ops-triage"},
    ],
    ids=["empty", "bad-service", "bad-region", "naive-time", "reversed", "over-31-days", "extra-app-id"],
)
async def test_invalid_arguments_are_rejected(billing_lookup, arguments):
    with pytest.raises(ValidationError):
        await billing_lookup.invoke(arguments)
