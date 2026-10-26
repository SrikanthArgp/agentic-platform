"""Daily token budgets per agent (Day 14, ADR-0029)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from app.governance.budget import BudgetExceededError, BudgetUnavailableError, TokenBudget, Usage
from app.kafka.alerts import handle_alert
from proto_gen import agent_pb2
from tests.conftest import (
    APP_ID,
    USAGE,
    FakeChatModel,
    FakeGateway,
    FakeRegistry,
    explanation,
    final,
    make_request,
    make_runner,
    tool_call,
)

pytestmark = pytest.mark.anyio

DAY = datetime(2026, 10, 26, 23, 59, 30, tzinfo=UTC)
PER_REPLY = USAGE["input_tokens"] + USAGE["output_tokens"]  # every scripted reply's usage


class Clock:
    def __init__(self, now: datetime = DAY):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def budget(redis=None, clock=None, default: int = 1000) -> TokenBudget:
    return TokenBudget(redis or FakeAsyncRedis(decode_responses=True), default_daily_tokens=default, clock=clock or Clock())


def down_redis() -> FakeAsyncRedis:
    server = FakeServer()
    server.connected = False
    return FakeAsyncRedis(server=server, decode_responses=True)


# --- TokenBudget ---


async def test_usage_accumulates_per_agent_with_input_and_output_apart():
    b = budget()
    await b.record(APP_ID, "triage-agent", Usage(100, 20))
    await b.record(APP_ID, "triage-agent", Usage(50, 5))
    await b.record(APP_ID, "summarizer", Usage(7, 3))

    assert await b.used(APP_ID, "triage-agent") == Usage(150, 25)
    assert await b.used(APP_ID, "summarizer") == Usage(7, 3)
    assert await b.used("other-app", "triage-agent") == Usage(0, 0)


async def test_key_is_per_utc_day_and_expires():
    redis = FakeAsyncRedis(decode_responses=True)
    b = budget(redis)
    await b.record(APP_ID, "triage-agent", Usage(1, 1))

    assert b.key(APP_ID, "triage-agent") == f"budget:{APP_ID}:triage-agent:2026-10-26"
    assert 0 < await redis.ttl(b.key(APP_ID, "triage-agent")) <= 2 * 24 * 3600


async def test_a_new_utc_day_starts_from_zero():
    clock = Clock()
    b = budget(clock=clock, default=100)
    await b.record(APP_ID, "triage-agent", Usage(100, 0))
    with pytest.raises(BudgetExceededError):
        await b.check(APP_ID, "triage-agent", None)

    clock.now += timedelta(seconds=31)  # 00:00:01 UTC the next day
    await b.check(APP_ID, "triage-agent", None)
    assert await b.used(APP_ID, "triage-agent") == Usage(0, 0)


async def test_utc_day_is_used_whatever_the_clocks_offset():
    # 01:00 UTC on the 27th is still 20:00 on the 26th in UTC-5.
    b = budget(clock=Clock(datetime(2026, 10, 26, 20, 0, tzinfo=timezone(timedelta(hours=-5)))))
    assert b.key(APP_ID, "a").endswith(":2026-10-27")


@pytest.mark.parametrize("used, limit, over", [(999, 1000, False), (1000, 1000, True), (1500, 1000, True), (0, 0, True)])
async def test_check_refuses_once_the_limit_is_reached(used, limit, over):
    b = budget(default=limit)
    await b.record(APP_ID, "triage-agent", Usage(used, 0))

    if over:
        with pytest.raises(BudgetExceededError, match=f"has used {used:,} of its {limit:,} daily tokens"):
            await b.check(APP_ID, "triage-agent", None)
    else:
        await b.check(APP_ID, "triage-agent", None)


async def test_the_manifests_daily_tokens_overrides_the_default():
    b = budget(default=1000)
    await b.record(APP_ID, "triage-agent", Usage(200, 0))

    await b.check(APP_ID, "triage-agent", None)
    with pytest.raises(BudgetExceededError):
        await b.check(APP_ID, "triage-agent", 200)


async def test_redis_down_check_is_unavailable_and_record_is_dropped():
    b = budget(down_redis())
    with pytest.raises(BudgetUnavailableError):
        await b.check(APP_ID, "triage-agent", None)
    await b.record(APP_ID, "triage-agent", Usage(5, 5))  # logged, not raised


# --- in a run ---


def registry_with_budgets(entry: int | None = None, summarizer: int | None = None, invoke: bool = False):
    registry = FakeRegistry()
    agents = registry.apps[APP_ID]["agents"]
    if entry is not None:
        agents[0]["budget"] = {"daily_tokens": entry}
    if summarizer is not None:
        agents[1]["budget"] = {"daily_tokens": summarizer}
    if invoke:
        agents[1]["invoke_on"] = ["ESCALATE"]
    return registry


async def test_a_run_records_each_agents_usage(apps_dir):
    b = budget()
    llm = FakeChatModel(responses=[tool_call(), final("ESCALATE", "needs a human"), explanation("why")])
    runner = make_runner(apps_dir, llm, registry=registry_with_budgets(invoke=True), budget=b)

    await runner.run(make_request())

    assert (await b.used(APP_ID, "triage-agent")).total == 2 * PER_REPLY
    assert (await b.used(APP_ID, "summarizer")).total == PER_REPLY


async def test_over_budget_entry_agent_is_not_run_and_the_alert_escalates_not_evaluated(apps_dir):
    b = budget()
    await b.record(APP_ID, "triage-agent", Usage(100, 0))
    llm, gateway = FakeChatModel(responses=[final()]), FakeGateway()
    runner = make_runner(apps_dir, llm, gateway, registry=registry_with_budgets(entry=100), budget=b)

    with pytest.raises(BudgetExceededError):
        await runner.run(make_request())
    _, response = await handle_alert(make_request().SerializeToString(), runner)

    assert llm.calls == [] and gateway.calls == []
    assert response.decision == agent_pb2.ESCALATE
    [reason] = response.reasons
    assert reason.startswith("orchestrator: not evaluated: budget exceeded: agent 'triage-agent'")
    assert "has used 100 of its 100 daily tokens" in reason


async def test_the_run_that_crosses_the_budget_finishes_and_the_next_is_refused(apps_dir):
    b = budget()
    registry = registry_with_budgets(entry=PER_REPLY)
    llm = FakeChatModel(responses=[tool_call(), final("ESCALATE", "first")])
    runner = make_runner(apps_dir, llm, registry=registry, budget=b)

    first = await runner.run(make_request())
    assert first.reasons[0] == "first"
    with pytest.raises(BudgetExceededError):
        await runner.run(make_request())


async def test_over_budget_callable_doesnt_contribute_and_the_decision_stands(apps_dir):
    b = budget()
    await b.record(APP_ID, "summarizer", Usage(10, 0))
    llm = FakeChatModel(responses=[final("ESCALATE", "needs a human")])
    runner = make_runner(apps_dir, llm, registry=registry_with_budgets(summarizer=10, invoke=True), budget=b)

    response = await runner.run(make_request())

    assert response.decision == agent_pb2.ESCALATE
    assert len(llm.calls) == 1  # the callable's model was never asked
    assert any(
        r.startswith("orchestrator: callable 'summarizer' didn't contribute (budget exceeded: agent 'summarizer'")
        for r in response.reasons
    )


async def test_redis_down_the_run_goes_ahead_and_says_the_budget_wasnt_checked(apps_dir):
    llm = FakeChatModel(responses=[tool_call(), final("AUTO_RESOLVE", "RB-001 says so")])
    runner = make_runner(apps_dir, llm, budget=budget(down_redis()))

    response = await runner.run(make_request())

    assert response.decision == agent_pb2.AUTO_RESOLVE
    assert any("token budget not checked (Redis unavailable" in r for r in response.reasons)
