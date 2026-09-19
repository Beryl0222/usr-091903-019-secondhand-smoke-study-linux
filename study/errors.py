"""领域错误类型与访问角色。"""


class StudyError(Exception):
    """所有领域错误的基类，携带稳定的机器可读代码。"""

    code = "study_error"
    http_status = 400

    def __init__(self, message, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self):
        payload = {"error": self.code, "message": self.message}
        payload.update(self.extra)
        return payload


class NotFound(StudyError):
    code = "not_found"
    http_status = 404


class Conflict(StudyError):
    code = "conflict"
    http_status = 409


class ValidationError(StudyError):
    code = "validation_error"
    http_status = 422


class CalibrationInvalid(StudyError):
    """读数存在但所用校准窗口已失效：保留数据，禁止进入正式估算。"""

    code = "calibration_invalid"
    http_status = 422


class PermissionDenied(StudyError):
    code = "permission_denied"
    http_status = 403


# 访问角色。社区与分析角色永远拿不到身份库的明文映射。
ROLES = ("field", "analyst", "community", "ethics", "admin")
