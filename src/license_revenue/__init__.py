"""成果许可与收益管理服务。

把权属依据、协议版本、许可、改进成果、分期收益和审计链绑定在同一个
SQLite 数据存储中，供技术转移办公室离线运行。
"""

from .clock import FrozenClock, SystemClock
from .errors import (
    Conflict,
    Forbidden,
    InvalidState,
    LicensingError,
    NotFound,
    ValidationFailed,
)
from .service import LicensingService

__all__ = [
    "LicensingService",
    "LicensingError",
    "NotFound",
    "Conflict",
    "InvalidState",
    "Forbidden",
    "ValidationFailed",
    "SystemClock",
    "FrozenClock",
]
