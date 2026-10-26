"""`billing-lookup`: what a service spent in a region over a window, broken
down by resource and by tag (docs/plan.md Day 13).

App-owned, read-only, fixture-backed (ADR-0004, ADR-0009), like
it-ops-triage's tools: `tool-gateway` imports this module by file path and
registers `TOOLS`; it must not import platform code.

The fixture (`billing.json`) stands in for a cloud billing export (AWS CUR,
GCP billing export): one line item per resource per day. Its input and
output shapes are meant to survive that swap. Dates are absolute, around
2026-10-01..08, as a real export's are.

A line item counts when its day starts inside [window_start, window_end):
billing is daily, so a window is effectively rounded to whole UTC days.
Tags are copied from the fixture as-is; whoever creates a resource sets
them, so they are claims about it, not proof.
"""

import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

FIXTURE_PATH = Path(__file__).with_name("billing.json")
MAX_WINDOW = timedelta(days=31)
# Resources listed in by_resource; the rest are summed into other_resources_cost_usd.
MAX_RESOURCES = 20


class BillingLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$", description="The alert's service, e.g. 'checkout-svc'.")
    region: str = Field(pattern=r"^[a-z]{2}-[a-z]+-[0-9]$", description="The alert's region, e.g. 'us-east-1'.")
    window_start: AwareDatetime = Field(
        description="Start of the window, ISO 8601 with a UTC offset, e.g. '2026-10-01T00:00:00Z'."
    )
    window_end: AwareDatetime = Field(description="End of the window (exclusive), ISO 8601 with a UTC offset.")

    @model_validator(mode="after")
    def _window(self) -> "BillingLookupInput":
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if self.window_end - self.window_start > MAX_WINDOW:
            raise ValueError("window is longer than 31 days")
        return self


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    service: str
    region: str
    resource_id: str
    resource_type: str
    cost_usd: float = Field(ge=0)
    tags: dict[str, str]


class ResourceCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str
    resource_type: str
    cost_usd: float
    tags: dict[str, str] = Field(description="As set on the resource; empty means untagged (no owner on record).")


class TagCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: str
    cost_usd: float


class BillingLookupOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    region: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    found: bool
    total_cost_usd: float = 0.0
    # Most expensive first.
    by_resource: list[ResourceCost] = []
    other_resources_cost_usd: float = 0.0
    # Spend per tag key=value, most expensive first; a resource counts once per tag it has.
    by_tag: list[TagCost] = []
    untagged_cost_usd: float = Field(default=0.0, description="Spend on resources with no tags at all.")
    message: str | None = None


def _load_items(path: Path) -> list[LineItem]:
    items = [LineItem.model_validate(i) for i in json.loads(path.read_text())]
    keys = [(i.date, i.resource_id) for i in items]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{path}: duplicate (date, resource_id)")
    return items


ITEMS = _load_items(FIXTURE_PATH)


def _day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def billing_lookup(args: BillingLookupInput) -> BillingLookupOutput:
    items = [
        i for i in ITEMS
        if i.service == args.service and i.region == args.region
        and args.window_start <= _day_start(i.date) < args.window_end
    ]
    window = {"service": args.service, "region": args.region,
              "window_start": args.window_start, "window_end": args.window_end}
    if not items:
        return BillingLookupOutput(
            **window, found=False,
            message=f"No billing data for '{args.service}' in {args.region} in this window.",
        )

    by_resource: dict[str, ResourceCost] = {}
    by_tag: dict[tuple[str, str], float] = defaultdict(float)
    for i in items:
        r = by_resource.setdefault(
            i.resource_id, ResourceCost(resource_id=i.resource_id, resource_type=i.resource_type, cost_usd=0, tags={})
        )
        r.cost_usd += i.cost_usd
        r.tags.update(i.tags)
        for kv in i.tags.items():
            by_tag[kv] += i.cost_usd

    resources = sorted(by_resource.values(), key=lambda r: (-r.cost_usd, r.resource_id))
    for r in resources:
        r.cost_usd = round(r.cost_usd, 2)
    return BillingLookupOutput(
        **window,
        found=True,
        total_cost_usd=round(sum(i.cost_usd for i in items), 2),
        by_resource=resources[:MAX_RESOURCES],
        other_resources_cost_usd=round(sum(r.cost_usd for r in resources[MAX_RESOURCES:]), 2),
        by_tag=[
            TagCost(key=k, value=v, cost_usd=round(c, 2))
            for (k, v), c in sorted(by_tag.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        untagged_cost_usd=round(sum(i.cost_usd for i in items if not i.tags), 2),
    )


TOOLS = {
    "billing-lookup": {
        "version": "1.0.0",
        "description": (
            "Break down what a service spent in a region between window_start and "
            "window_end: total, per resource (with its tags), per tag, and how much was "
            "untagged. Billing is daily, so the window counts whole UTC days. Returns "
            "found=false when there is no billing data for that service, region and window."
        ),
        "input_model": BillingLookupInput,
        "output_model": BillingLookupOutput,
        "handler": billing_lookup,
        "read_only": True,
    },
}
