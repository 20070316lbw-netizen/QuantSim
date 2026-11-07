"""提供 Simulator 调用方可判断的错误代码

包装外部异常时保留原始原因。

    try:
        quantity = int(raw_quantity)
    except ValueError as exc:
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "股数须为整数") from exc
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
    CAPABILITY_DISABLED = "CAPABILITY_DISABLED"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_LINEAGE = "INVALID_LINEAGE"
    DATA_NOT_VISIBLE = "DATA_NOT_VISIBLE"
    TOOLKIT_INPUT_REJECTED = "TOOLKIT_INPUT_REJECTED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    TOOLKIT_FAILURE = "TOOLKIT_FAILURE"
    INCONSISTENT_STATE = "INCONSISTENT_STATE"


class SimulatorError(Exception):
    def __init__(
        self, code: ErrorCode, message: str, *, capability: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.capability = capability
