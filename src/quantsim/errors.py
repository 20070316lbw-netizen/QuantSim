"""统一暴露模拟器错误代码和异常

ErrorCode 定义调用方可识别的错误类别
SimulatorError.__init__ 保存错误代码和可选能力名, 由行情、策略和撮合等模块调用
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
