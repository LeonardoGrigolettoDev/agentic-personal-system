"""Errors that map to HTTP statuses. Messages are user-facing (pt-BR) and never contain secrets."""


class EdgeError(Exception):
    status = 500

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"status": self.status, "error": self.message}


class BadRequest(EdgeError):
    status = 400


class Forbidden(EdgeError):
    status = 403


class NotFound(EdgeError):
    status = 404


class TooLarge(EdgeError):
    status = 413


class UnsupportedMedia(EdgeError):
    status = 415


class Unprocessable(EdgeError):
    status = 422


class Busy(EdgeError):
    status = 429


class NotImplementedFeature(EdgeError):
    status = 501


class UpstreamError(EdgeError):
    status = 502


class Unavailable(EdgeError):
    status = 503


class UpstreamTimeout(EdgeError):
    status = 504
