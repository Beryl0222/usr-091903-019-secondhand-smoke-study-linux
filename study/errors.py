"""领域与接口层共用的异常类型。"""


class StudyError(Exception):
    """所有可预期业务错误的基类，message 直接返回给调用方。"""

    status = 400

    def __init__(self, message, status=None):
        super().__init__(message)
        if status is not None:
            self.status = status


class ValidationError(StudyError):
    status = 400


class NotFoundError(StudyError):
    status = 404


class ConflictError(StudyError):
    status = 409


class AuthorizationError(StudyError):
    status = 403


class ImmutableVersionError(StudyError):
    """冻结版本一经创建即不可修改。"""

    status = 409
