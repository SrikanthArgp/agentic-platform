import textwrap
from pathlib import Path

import pytest

# The real backend/apps directory, for tests of app-owned tools.
REPO_APPS_DIR = Path(__file__).resolve().parents[3] / "apps"

ECHO_TOOL_MODULE = """
from pydantic import BaseModel, ConfigDict


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class EchoOutput(BaseModel):
    echoed: str


def echo(args: EchoInput) -> EchoOutput:
    return EchoOutput(echoed=args.text)


async def async_echo(args: EchoInput) -> EchoOutput:
    return EchoOutput(echoed=args.text.upper())


def broken(args: EchoInput) -> EchoOutput:
    raise RuntimeError("fixture backend unavailable")


def wrong_shape(args: EchoInput) -> dict:
    return {"echoed": args.text}


def _tool(handler):
    return {
        "version": "0.1.0",
        "description": "Echo the input back.",
        "input_model": EchoInput,
        "output_model": EchoOutput,
        "handler": handler,
    }


TOOLS = {
    "{prefix}echo": _tool(echo),
    "{prefix}async_echo": _tool(async_echo),
    "{prefix}broken": _tool(broken),
    "{prefix}wrong_shape": _tool(wrong_shape),
}
"""


def write_tool_module(apps_dir: Path, app_id: str, module: str, source: str) -> Path:
    tools_dir = apps_dir / app_id / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    path = tools_dir / f"{module}.py"
    path.write_text(textwrap.dedent(source))
    return path


def echo_module(prefix: str = "") -> str:
    return ECHO_TOOL_MODULE.replace("{prefix}", prefix)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
