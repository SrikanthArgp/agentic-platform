"""`TOOL_FAULTS`: deliberately broken tools for live resilience tests."""

import time

import pytest

from app.core.faults import InjectedFaultError, apply_faults
from app.core.loader import load_app_tools
from app.core.registry import ToolRegistry
from tests.conftest import echo_module, write_tool_module

pytestmark = pytest.mark.anyio


@pytest.fixture
def registry(tmp_path):
    write_tool_module(tmp_path, "app-one", "echo_tools", echo_module())
    registry = ToolRegistry()
    load_app_tools(tmp_path, registry)
    return registry


async def test_error_fault_makes_the_tool_raise(registry):
    broken = apply_faults("echo=error", registry)

    with pytest.raises(InjectedFaultError):
        await broken.get("echo").invoke({"text": "hi"})
    # Other tools, and the original registry, are untouched.
    assert (await broken.get("async_echo").invoke({"text": "hi"})).echoed == "HI"
    assert (await registry.get("echo").invoke({"text": "hi"})).echoed == "hi"


@pytest.mark.parametrize("tool, expected", [("echo", "hi"), ("async_echo", "HI")])
async def test_sleep_fault_delays_then_runs_the_tool(registry, tool, expected):
    slowed = apply_faults(f"{tool}=sleep:0.05", registry)
    started = time.monotonic()

    assert (await slowed.get(tool).invoke({"text": "hi"})).echoed == expected
    assert time.monotonic() - started >= 0.05


@pytest.mark.parametrize(
    "spec", ["echo", "echo=boom", "echo=sleep:x", "echo=sleep:-1", "no_such_tool=error"]
)
def test_bad_spec_fails_startup(registry, spec):
    with pytest.raises(ValueError):
        apply_faults(spec, registry)
