from typing import Optional, Any


class VKCaptchaSolverError(Exception):
    """Base exception for VK Captcha Solver."""

    pass


class HTTPError(VKCaptchaSolverError):
    """Exception raised for HTTP errors."""

    def __init__(
        self, status: int, status_text: str, url: str, message: Optional[str] = None
    ):
        self.status = status
        self.status_text = status_text
        self.url = url
        super().__init__(message or f"HTTP Error {status}: {status_text}")


class APIError(VKCaptchaSolverError):
    """Exception raised for API errors."""

    def __init__(self, error: Any, method: str):
        self.error = error
        self.method = method
        self.code: Optional[int] = error.get("error_code")
        self.status: Optional[str] = error.get("status")

        error_msg = error.get("error_msg", "Unknown error")

        if self.status is not None:
            message = f"[Status: {self.status}] ({method}) {error_msg}"
        else:
            message = f"[Code: {self.code}] ({method}) {error_msg}"

        super().__init__(message)
