"""成果许可服务向 API 和 CLI 暴露的稳定错误。"""


class LicensingError(RuntimeError):
    code = "licensing_error"
    status = 400


class NotFound(LicensingError):
    code = "not_found"
    status = 404


class Conflict(LicensingError):
    code = "conflict"
    status = 409

    def __init__(self, message: str = "冲突", details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class Forbidden(LicensingError):
    code = "forbidden"
    status = 403


class InvalidState(LicensingError):
    code = "invalid_state"
    status = 409


class ValidationFailed(LicensingError):
    code = "validation_failed"
    status = 422
