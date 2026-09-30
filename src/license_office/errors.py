"""成果许可服务向 API 和 CLI 暴露的稳定错误。"""


class LicenseOfficeError(RuntimeError):
    code = "license_office_error"
    status = 400


class NotFound(LicenseOfficeError):
    code = "not_found"
    status = 404


class Conflict(LicenseOfficeError):
    code = "conflict"
    status = 409


class Forbidden(LicenseOfficeError):
    code = "forbidden"
    status = 403


class InvalidState(LicenseOfficeError):
    code = "invalid_state"
    status = 409


class ValidationFailed(LicenseOfficeError):
    code = "validation_failed"
    status = 422
