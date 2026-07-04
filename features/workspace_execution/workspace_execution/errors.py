"""Structured errors for the workspace_execution feature."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from workspace_execution.types import ErrorDetail, ErrorResponse


class WorkspaceExecutionError(Exception):
    """Feature error that can be converted to the public error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = HTTPStatus.BAD_REQUEST,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = int(status_code)
        self.details = details or {}


def error_response(error: WorkspaceExecutionError) -> ErrorResponse:
    return ErrorResponse(
        success=False,
        error=ErrorDetail(
            code=error.code,
            message=error.message,
            details=error.details,
        ),
    )


async def workspace_execution_error_handler(
    _request: Request,
    error: WorkspaceExecutionError,
) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=error_response(error).model_dump(mode="json"),
    )
