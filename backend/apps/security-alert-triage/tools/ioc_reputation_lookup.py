"""`ioc-reputation-lookup`: what threat intel and the asset inventory say
about one indicator of compromise (docs/plan.md Day 20).

App-owned, read-only, fixture-backed (ADR-0004, ADR-0009), like the other
apps' tools: `tool-gateway` imports this module by file path and registers
`TOOLS`; it must not import platform code. No live threat-intel API is
called. The fixture (`ioc_reputation.json`) stands in for a threat-intel
feed plus an asset inventory; every entry is synthetic (documentation IP
ranges, `.example` domains).

The indicator comes from the alert, so it is attacker-chosen text. It's
only ever normalised and compared against the fixture: IPs canonicalised
(`ipaddress`), domains and hashes lower-cased, a trailing dot dropped.
Anything not in the fixture is `unknown`, which is not the same as benign.
"""

import ipaddress
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FIXTURE_PATH = Path(__file__).with_name("ioc_reputation.json")

IndicatorType = Literal["ip", "domain", "file_hash"]
Verdict = Literal["known-bad", "known-benign", "unknown"]

_HASH = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$")  # MD5, SHA-1, SHA-256
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")


class IocReputationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indicator: str = Field(
        min_length=1, max_length=256,
        description="One indicator from the alert: an IP address, a domain, or a file hash (MD5/SHA-1/SHA-256).",
    )


class IocReputationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indicator: str = Field(description="The indicator as looked up (normalised).")
    type: IndicatorType | Literal["unrecognized"]
    verdict: Verdict = Field(description="'unknown' means no record either way; it is not evidence of benign.")
    source: str | None = Field(default=None, description="Where the verdict comes from, e.g. 'threat-intel-feed'.")
    description: str | None = None


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: IndicatorType
    value: str
    verdict: Literal["known-bad", "known-benign"]
    source: str
    description: str


def normalise(raw: str) -> tuple[str, IndicatorType | Literal["unrecognized"]]:
    value = raw.strip()
    try:
        return str(ipaddress.ip_address(value)), "ip"
    except ValueError:
        pass
    lowered = value.lower().rstrip(".")
    if _HASH.match(lowered):
        return lowered, "file_hash"
    if _DOMAIN.match(lowered):
        return lowered, "domain"
    return value, "unrecognized"


def _load(path: Path) -> dict[tuple[str, str], Entry]:
    entries = {}
    for raw in json.loads(path.read_text()):
        entry = Entry.model_validate(raw)
        value, kind = normalise(entry.value)
        if kind != entry.type or value != entry.value:
            raise ValueError(f"{path}: {entry.value!r} isn't a normalised {entry.type}")
        if (kind, value) in entries:
            raise ValueError(f"{path}: duplicate indicator {value!r}")
        entries[(kind, value)] = entry
    return entries


ENTRIES = _load(FIXTURE_PATH)


def ioc_reputation_lookup(args: IocReputationInput) -> IocReputationOutput:
    value, kind = normalise(args.indicator)
    entry = ENTRIES.get((kind, value))
    if entry is None:
        return IocReputationOutput(indicator=value, type=kind, verdict="unknown")
    return IocReputationOutput(
        indicator=value, type=kind, verdict=entry.verdict, source=entry.source, description=entry.description
    )


TOOLS = {
    "ioc-reputation-lookup": {
        "version": "1.0.0",
        "description": (
            "Look up one indicator of compromise (an IP address, a domain, or a file hash) "
            "in threat intel and the asset inventory. Returns verdict known-bad, known-benign "
            "or unknown, with its source and a description. 'unknown' means no record either "
            "way, not that the indicator is safe. Call it once per indicator."
        ),
        "input_model": IocReputationInput,
        "output_model": IocReputationOutput,
        "handler": ioc_reputation_lookup,
        "read_only": True,
    },
}
