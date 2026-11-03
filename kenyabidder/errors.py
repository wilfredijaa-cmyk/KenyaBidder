class AppError(Exception):
    """Domain error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __repr__(self) -> str:
        return f"AppError({self.code}: {self.message})"


def bad(code: str, message: str) -> AppError:
    return AppError(code, message, 400)


def forbidden(code: str, message: str) -> AppError:
    return AppError(code, message, 403)


def not_found(code: str, message: str) -> AppError:
    return AppError(code, message, 404)


def conflict(code: str, message: str) -> AppError:
    return AppError(code, message, 409)


def is_int(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool)


def is_pos_int(n) -> bool:
    return is_int(n) and n > 0


def is_nonneg_int(n) -> bool:
    return is_int(n) and n >= 0
