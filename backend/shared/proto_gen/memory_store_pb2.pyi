from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GetContextRequest(_message.Message):
    __slots__ = ("app_id", "alert_key")
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    ALERT_KEY_FIELD_NUMBER: _ClassVar[int]
    app_id: str
    alert_key: str
    def __init__(self, app_id: _Optional[str] = ..., alert_key: _Optional[str] = ...) -> None: ...

class ContextAggregate(_message.Message):
    __slots__ = ("alert_count", "escalation_count", "suppression_count", "confirmed_incident_count", "confirmed_noise_count")
    ALERT_COUNT_FIELD_NUMBER: _ClassVar[int]
    ESCALATION_COUNT_FIELD_NUMBER: _ClassVar[int]
    SUPPRESSION_COUNT_FIELD_NUMBER: _ClassVar[int]
    CONFIRMED_INCIDENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    CONFIRMED_NOISE_COUNT_FIELD_NUMBER: _ClassVar[int]
    alert_count: int
    escalation_count: int
    suppression_count: int
    confirmed_incident_count: int
    confirmed_noise_count: int
    def __init__(self, alert_count: _Optional[int] = ..., escalation_count: _Optional[int] = ..., suppression_count: _Optional[int] = ..., confirmed_incident_count: _Optional[int] = ..., confirmed_noise_count: _Optional[int] = ...) -> None: ...

class GetContextResponse(_message.Message):
    __slots__ = ("app_id", "alert_key", "window_1h", "window_24h", "window_7d", "is_novel_alert", "has_confirmed_incident_history")
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    ALERT_KEY_FIELD_NUMBER: _ClassVar[int]
    WINDOW_1H_FIELD_NUMBER: _ClassVar[int]
    WINDOW_24H_FIELD_NUMBER: _ClassVar[int]
    WINDOW_7D_FIELD_NUMBER: _ClassVar[int]
    IS_NOVEL_ALERT_FIELD_NUMBER: _ClassVar[int]
    HAS_CONFIRMED_INCIDENT_HISTORY_FIELD_NUMBER: _ClassVar[int]
    app_id: str
    alert_key: str
    window_1h: ContextAggregate
    window_24h: ContextAggregate
    window_7d: ContextAggregate
    is_novel_alert: bool
    has_confirmed_incident_history: bool
    def __init__(self, app_id: _Optional[str] = ..., alert_key: _Optional[str] = ..., window_1h: _Optional[_Union[ContextAggregate, _Mapping]] = ..., window_24h: _Optional[_Union[ContextAggregate, _Mapping]] = ..., window_7d: _Optional[_Union[ContextAggregate, _Mapping]] = ..., is_novel_alert: _Optional[bool] = ..., has_confirmed_incident_history: _Optional[bool] = ...) -> None: ...
