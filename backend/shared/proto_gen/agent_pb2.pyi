from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Decision(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DECISION_UNSPECIFIED: _ClassVar[Decision]
    AUTO_RESOLVE: _ClassVar[Decision]
    ESCALATE: _ClassVar[Decision]
    SUPPRESS: _ClassVar[Decision]
DECISION_UNSPECIFIED: Decision
AUTO_RESOLVE: Decision
ESCALATE: Decision
SUPPRESS: Decision

class RunAgentRequest(_message.Message):
    __slots__ = ("app_id", "agent_id", "alert_id", "alert_key", "source", "severity", "message", "timestamp_unix_ms", "payload")
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    ALERT_ID_FIELD_NUMBER: _ClassVar[int]
    ALERT_KEY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SEVERITY_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    app_id: str
    agent_id: str
    alert_id: str
    alert_key: str
    source: str
    severity: str
    message: str
    timestamp_unix_ms: int
    payload: _struct_pb2.Struct
    def __init__(self, app_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., alert_id: _Optional[str] = ..., alert_key: _Optional[str] = ..., source: _Optional[str] = ..., severity: _Optional[str] = ..., message: _Optional[str] = ..., timestamp_unix_ms: _Optional[int] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ToolCall(_message.Message):
    __slots__ = ("tool_name", "result_summary", "agent_id")
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    RESULT_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    tool_name: str
    result_summary: str
    agent_id: str
    def __init__(self, tool_name: _Optional[str] = ..., result_summary: _Optional[str] = ..., agent_id: _Optional[str] = ...) -> None: ...

class RunAgentResponse(_message.Message):
    __slots__ = ("app_id", "agent_id", "alert_id", "decision", "reasons", "tool_calls", "alert_key", "confidence", "alert")
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    ALERT_ID_FIELD_NUMBER: _ClassVar[int]
    DECISION_FIELD_NUMBER: _ClassVar[int]
    REASONS_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    ALERT_KEY_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    ALERT_FIELD_NUMBER: _ClassVar[int]
    app_id: str
    agent_id: str
    alert_id: str
    decision: Decision
    reasons: _containers.RepeatedScalarFieldContainer[str]
    tool_calls: _containers.RepeatedCompositeFieldContainer[ToolCall]
    alert_key: str
    confidence: float
    alert: RunAgentRequest
    def __init__(self, app_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., alert_id: _Optional[str] = ..., decision: _Optional[_Union[Decision, str]] = ..., reasons: _Optional[_Iterable[str]] = ..., tool_calls: _Optional[_Iterable[_Union[ToolCall, _Mapping]]] = ..., alert_key: _Optional[str] = ..., confidence: _Optional[float] = ..., alert: _Optional[_Union[RunAgentRequest, _Mapping]] = ...) -> None: ...
