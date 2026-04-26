"""Custom exceptions and a DRF exception handler that maps them to JSON errors.

Why a custom handler? DRF's default returns 500 for arbitrary exceptions. We
want crisp, deterministic 4xx responses for known business-rule violations
(insufficient balance, illegal state transition, idempotency conflict).
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class LedgerError(Exception):
    """Base for all expected, user-facing ledger errors."""

    http_status = status.HTTP_400_BAD_REQUEST
    code = "ledger_error"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class InsufficientBalance(LedgerError):
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "insufficient_balance"


class InvalidTransition(LedgerError):
    http_status = status.HTTP_409_CONFLICT
    code = "invalid_transition"


class IdempotencyConflict(LedgerError):
    """Same key replayed with a different request body."""

    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "idempotency_conflict"


class IdempotencyInFlight(LedgerError):
    """Same key seen but the original request hasn't finished yet."""

    http_status = status.HTTP_409_CONFLICT
    code = "idempotency_in_flight"


def api_exception_handler(exc, context):
    if isinstance(exc, LedgerError):
        body = {"error": {"code": exc.code, "message": exc.message, **exc.extra}}
        return Response(body, status=exc.http_status)
    return drf_exception_handler(exc, context)
