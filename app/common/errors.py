from typing import Any


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: Any = None) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        super().__init__(code)
