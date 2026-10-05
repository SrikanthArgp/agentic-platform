from pathlib import Path

import pytest

from app.core.loader import ToolLoadError, load_app_tools
from app.core.registry import ToolNotFoundError, ToolRegistry
from tests.conftest import echo_module, write_tool_module


def load(apps_dir: Path) -> ToolRegistry:
    registry = ToolRegistry()
    load_app_tools(apps_dir, registry)
    return registry


def test_discovers_tools_from_every_app_and_records_owner(tmp_path):
    write_tool_module(tmp_path, "app-one", "echo_tools", echo_module("one_"))
    write_tool_module(tmp_path, "app-two", "echo_tools", echo_module("two_"))

    registry = load(tmp_path)

    assert registry.get("one_echo").app_id == "app-one"
    assert registry.get("two_echo").app_id == "app-two"
    assert registry.get("one_echo").scope == "app"
    assert len(registry) == 8


def test_same_module_name_in_two_apps_does_not_collide(tmp_path):
    # Both apps have tools/echo_tools.py; each must be imported separately.
    write_tool_module(tmp_path, "app-one", "echo_tools", echo_module("one_"))
    write_tool_module(tmp_path, "app-two", "echo_tools", echo_module("two_"))

    registry = load(tmp_path)

    assert registry.get("one_echo").handler is not registry.get("two_echo").handler


def test_skips_underscore_modules_and_apps_without_tools(tmp_path):
    write_tool_module(tmp_path, "app-one", "_helpers", "raise RuntimeError('must not be imported')")
    write_tool_module(tmp_path, "app-one", "echo_tools", echo_module())
    (tmp_path / "app-without-tools").mkdir()

    assert load(tmp_path).get("echo").tool_id == "echo"


def test_empty_apps_dir_loads_nothing(tmp_path):
    assert len(load(tmp_path)) == 0


def test_unknown_tool_id_is_an_explicit_not_found(tmp_path):
    write_tool_module(tmp_path, "app-one", "echo_tools", echo_module())

    with pytest.raises(ToolNotFoundError) as exc:
        load(tmp_path).get("no_such_tool")
    assert exc.value.tool_id == "no_such_tool"


def test_missing_apps_dir_fails(tmp_path):
    with pytest.raises(ToolLoadError, match="does not exist"):
        load(tmp_path / "nope")


def test_duplicate_tool_id_across_apps_fails(tmp_path):
    write_tool_module(tmp_path, "app-one", "echo_tools", echo_module())
    write_tool_module(tmp_path, "app-two", "echo_tools", echo_module())

    with pytest.raises(ToolLoadError, match="registered twice"):
        load(tmp_path)


def test_import_error_fails_and_names_the_file(tmp_path):
    path = write_tool_module(tmp_path, "app-one", "bad", "import no_such_module_xyz")

    with pytest.raises(ToolLoadError, match="import failed") as exc:
        load(tmp_path)
    assert str(path) in str(exc.value)


def test_module_without_tools_dict_fails(tmp_path):
    write_tool_module(tmp_path, "app-one", "no_tools", "X = 1")

    with pytest.raises(ToolLoadError, match="must export TOOLS"):
        load(tmp_path)


@pytest.mark.parametrize(
    "tools_source, error",
    [
        ('TOOLS = {"Bad Id": {}}', "tool_id must match"),
        ('TOOLS = {"t": "not a dict"}', "definition must be a dict"),
        ('TOOLS = {"t": {"version": "1"}}', "missing keys"),
        (
            'TOOLS = {"t": {"version": "1", "description": "d", "input_model": dict, '
            '"output_model": dict, "handler": print, "read_only": True}}',
            "input_model must be a pydantic BaseModel subclass",
        ),
    ],
)
def test_malformed_tool_definition_fails(tmp_path, tools_source, error):
    write_tool_module(tmp_path, "app-one", "bad", tools_source)

    with pytest.raises(ToolLoadError, match=error):
        load(tmp_path)


def test_tool_not_declared_read_only_fails(tmp_path):
    source = echo_module().replace('"read_only": True,', '"read_only": False,')
    write_tool_module(tmp_path, "app-one", "echo_tools", source)

    with pytest.raises(ToolLoadError, match="read_only must be True"):
        load(tmp_path)


def test_unknown_definition_key_fails(tmp_path):
    source = echo_module().replace('"handler": handler,', '"handler": handler, "app_id": "x",')
    write_tool_module(tmp_path, "app-one", "echo_tools", source)

    with pytest.raises(ToolLoadError, match=r"unknown keys \['app_id'\]"):
        load(tmp_path)
