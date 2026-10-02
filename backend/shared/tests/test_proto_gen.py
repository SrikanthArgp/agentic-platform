"""Guards against codegen drift: generated stubs must import cleanly and
their message fields must match the .proto source of truth field-for-field.
Catches a stale `proto_gen/` (edited .proto, forgot to rerun gen_proto.sh)
before any real handler code depends on the wrong shape.

The stubs are committed, so this always runs; the importorskip guards
below only matter if `proto_gen/` is ever emptied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("proto_gen.memory_store_pb2")
pytest.importorskip("proto_gen.agent_pb2")

from proto_gen import agent_pb2, memory_store_pb2  # noqa: E402


def _fields_from_proto_source(proto_path: Path, message_name: str) -> list[str]:
    text = proto_path.read_text()
    start = text.index(f"message {message_name} {{")
    end = text.index("}", start)
    body = text[start:end]
    fields = []
    for line in body.splitlines():
        line = line.strip().rstrip(";")
        if not line or line.startswith("//"):
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[-2] == "=":
            fields.append(parts[-3])
    return fields


def test_memory_store_stubs_import():
    req = memory_store_pb2.GetContextRequest(alert_key="k1")
    assert req.alert_key == "k1"


def test_agent_stubs_import():
    resp = agent_pb2.RunAgentResponse(alert_id="a1", reasons=["novel_alert_key"])
    assert resp.reasons == ["novel_alert_key"]


def test_memory_store_request_fields_match_proto_source():
    proto_path = Path(__file__).resolve().parents[2] / "proto" / "memory_store.proto"
    expected = _fields_from_proto_source(proto_path, "GetContextRequest")
    actual = [f.name for f in memory_store_pb2.GetContextRequest.DESCRIPTOR.fields]
    assert actual == expected


def test_agent_request_fields_match_proto_source():
    proto_path = Path(__file__).resolve().parents[2] / "proto" / "agent.proto"
    expected = _fields_from_proto_source(proto_path, "RunAgentRequest")
    actual = [f.name for f in agent_pb2.RunAgentRequest.DESCRIPTOR.fields]
    assert actual == expected


def test_agent_response_fields_match_proto_source():
    proto_path = Path(__file__).resolve().parents[2] / "proto" / "agent.proto"
    expected = _fields_from_proto_source(proto_path, "RunAgentResponse")
    actual = [f.name for f in agent_pb2.RunAgentResponse.DESCRIPTOR.fields]
    assert actual == expected
