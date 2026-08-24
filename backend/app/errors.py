"""Domain exceptions, mapped to HTTP status codes by a handler in main.py.

Keeping these framework-agnostic lets the service layer stay free of FastAPI.
"""
from __future__ import annotations


class DomainError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class BadRequest(DomainError):
    status_code = 400


class Unauthorized(DomainError):
    status_code = 401


class Forbidden(DomainError):
    status_code = 403


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class UnprocessableEntity(DomainError):
    """Well-formed request, semantically invalid field.

    Matches the 422 FastAPI already returns for schema violations, so a bad
    email looks the same whether it was caught by Pydantic on a JSON route or
    by the service layer on an HTML form post.
    """

    status_code = 422
