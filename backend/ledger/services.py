"""Business logic that multiple call sites need to agree on.

Why a `services` module instead of fat models or fat views? Because the
correctness of a payout system lives in transactions, not single rows. The
critical operations — creating a payout, marking one completed, marking one
failed — each span multiple tables and need to happen atomically. Keeping
them here makes the locking strategy explicit and reusable.

Two non-obvious things to know when reading this file:

1. `select_for_update()` translates to `SELECT ... FOR UPDATE` in Postgres,
   acquiring a row-level exclusive lock that blocks any other transaction
   that does the same SELECT FOR UPDATE on the same row, until the first
   transaction commits. We always lock the Merchant row before touching the
   merchant's balance. Two concurrent payout requests for the same merchant
   serialize on this lock — that's how we prevent double-spend.

2. The balance is never stored. It's recomputed via a single SQL aggregation
   so the database is always the source of truth. Storing balance and
   maintaining it would mean two writes per debit (entry + balance), and any
   bug between them produces a phantom rupee.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .exceptions import (
    IdempotencyConflict,
    IdempotencyInFlight,
    InsufficientBalance,
    LedgerError,
)
from .models import IdempotencyKey, LedgerEntry, Merchant, Payout

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Balance:
    """Snapshot of a merchant's money. All values in paise."""

    settled: int  # SUM(CREDIT) - SUM(DEBIT)
    held: int  # SUM(amount_paise) of payouts in pending|processing
    available: int  # settled - held


def compute_balance(merchant_id) -> Balance:
    """Compute the merchant's balance from the database in one round trip.

    This is the only place balance comes from. Notice we do not fetch ledger
    rows into Python and add them up — that would be wrong on two counts:
    it costs O(n) memory, and it's racy (rows can be inserted between the
    fetch and the sum). The database does the aggregation under whatever
    locks the surrounding transaction holds.
    """
    settled = (
        LedgerEntry.objects.filter(merchant_id=merchant_id)
        .aggregate(
            credits=Sum("amount_paise", filter=Q(kind=LedgerEntry.Kind.CREDIT)),
            debits=Sum("amount_paise", filter=Q(kind=LedgerEntry.Kind.DEBIT)),
        )
    )
    credits = settled["credits"] or 0
    debits = settled["debits"] or 0

    held = (
        Payout.objects.filter(
            merchant_id=merchant_id,
            status__in=[Payout.Status.PENDING, Payout.Status.PROCESSING],
        ).aggregate(total=Sum("amount_paise"))["total"]
        or 0
    )

    settled_balance = credits - debits
    return Balance(
        settled=settled_balance,
        held=held,
        available=settled_balance - held,
    )


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def lock_merchant(merchant_id) -> Merchant:
    """Acquire a row-level exclusive lock on the merchant row.

    MUST be called inside a transaction. The lock is released when the
    transaction commits or rolls back. Postgres serializes any other
    transaction that calls this on the same merchant — they will block here
    until we commit.

    This is the database primitive that prevents the classic
    check-then-deduct race: thread A reads balance=100, thread B reads
    balance=100, both see "enough", both create a 60-rupee payout. With the
    lock, B's SELECT blocks until A commits (or rolls back), and then B sees
    the post-A state and correctly rejects.
    """
    return Merchant.objects.select_for_update().get(pk=merchant_id)


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------


@transaction.atomic
def create_payout(
    *, merchant_id, bank_account_id, amount_paise: int
) -> Payout:
    """Create a payout in `pending` state, holding the funds.

    Whole flow runs inside one transaction:
        1. SELECT FOR UPDATE on the merchant row.
        2. Recompute balance from the database.
        3. If available < amount, raise InsufficientBalance and abort.
        4. Otherwise INSERT the payout row.
    Once we commit, the new payout is `pending` and counts toward `held`,
    so subsequent balance reads will see the funds reserved.

    The `@transaction.atomic` decorator wraps everything in a Postgres
    transaction. The merchant lock is released on commit/rollback.
    """
    if amount_paise <= 0:
        raise LedgerError("amount must be positive")

    merchant = lock_merchant(merchant_id)
    balance = compute_balance(merchant.id)

    if balance.available < amount_paise:
        raise InsufficientBalance(
            "available balance is less than payout amount",
            available_paise=balance.available,
            requested_paise=amount_paise,
        )

    return Payout.objects.create(
        merchant=merchant,
        bank_account_id=bank_account_id,
        amount_paise=amount_paise,
        status=Payout.Status.PENDING,
    )


@transaction.atomic
def begin_processing(payout_id) -> Payout | None:
    """Move a payout from pending to processing, recording the start time.

    Returns the payout if we successfully transitioned, or None if the row
    is no longer pending (already picked up, already terminal, etc.). The
    caller — the worker — should handle the None case by simply returning;
    something else is, or has, processed this row.

    We lock the payout row itself here (not the merchant) because we are
    only mutating the payout. No balance change happens on this transition.
    """
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    if payout.status != Payout.Status.PENDING:
        # Could be a duplicate worker pickup or a retry that already moved on.
        return None
    payout.transition_to(Payout.Status.PROCESSING)
    payout.processing_started_at = timezone.now()
    payout.attempts = (payout.attempts or 0) + 1
    payout.save(
        update_fields=["status", "processing_started_at", "attempts", "updated_at"]
    )
    return payout


@transaction.atomic
def restart_processing(payout_id) -> Payout | None:
    """Re-arm a stuck payout for another attempt.

    The state stays PROCESSING (per the legal-transitions table, you cannot
    leave PROCESSING except to a terminal state). We just refresh
    `processing_started_at` so the reaper doesn't immediately pick it up
    again, and bump `attempts`.
    """
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    if payout.status != Payout.Status.PROCESSING:
        return None
    payout.attempts = (payout.attempts or 0) + 1
    payout.processing_started_at = timezone.now()
    payout.save(update_fields=["processing_started_at", "attempts", "updated_at"])
    return payout


@transaction.atomic
def mark_completed(payout_id) -> None:
    """Finalize a successful payout.

    Two writes inside one transaction:
        - flip status PROCESSING -> COMPLETED (state machine guard inside)
        - INSERT a DEBIT ledger entry for the same amount

    Both succeed or both roll back. If we crashed between them we'd have a
    payout marked complete with no corresponding debit — i.e., free money
    for the merchant. Atomicity is not optional here.
    """
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    payout.transition_to(Payout.Status.COMPLETED)
    payout.completed_at = timezone.now()
    payout.save(update_fields=["status", "completed_at", "updated_at"])
    LedgerEntry.objects.create(
        merchant=payout.merchant,
        kind=LedgerEntry.Kind.DEBIT,
        amount_paise=payout.amount_paise,
        description=f"Payout {payout.id}",
        payout=payout,
    )


@transaction.atomic
def mark_failed(payout_id, *, reason: str) -> None:
    """Mark a payout as failed and release the held funds.

    Holding funds is implicit (a `pending`/`processing` payout counts toward
    `held`). So "releasing the funds" is just changing status to `failed`.
    No ledger entry is needed: no money actually moved. The state change
    itself, atomic by virtue of being one row UPDATE inside a transaction,
    *is* the release.
    """
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    payout.transition_to(Payout.Status.FAILED)
    payout.failure_reason = reason[:255]
    payout.save(update_fields=["status", "failure_reason", "updated_at"])


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonicalized JSON payload.

    Canonical = sorted keys + no whitespace. So `{"a":1,"b":2}` and
    `{"b": 2, "a": 1}` produce the same fingerprint. This lets us detect
    when a client genuinely sends a different payload with the same key
    (which is a client bug we should refuse).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def idempotent(
    *,
    merchant_id,
    key: str,
    request_payload: dict[str, Any],
    handler: Callable[[], tuple[int, dict[str, Any], Payout | None]],
) -> tuple[int, dict[str, Any]]:
    """Run `handler` at most once per (merchant, key) and cache its response.

    `handler` is the actual business logic. It returns a (http_status, body,
    payout_or_none) triple. We persist the body so a replay returns it
    byte-for-byte (well, JSON-for-JSON).

    The race we care about:
        request A and request B arrive within ms of each other with the
        same key. They both reach this function at the same time.

    How it serializes:
        Each tries `INSERT ... (merchant, key, fingerprint)`. The UNIQUE
        constraint on (merchant, key) means exactly one INSERT wins. The
        loser raises IntegrityError, reads the existing row, and either
        returns the cached response (if the winner already finished) or
        refuses with 409 IdempotencyInFlight (if not).

    Why an immediately-committed INSERT and not a SELECT-then-INSERT?
        Because SELECT-then-INSERT has the exact race we're trying to
        prevent. The unique constraint is the only thing that's atomic
        across connections.

    Expiry: rows older than IDEMPOTENCY_KEY_TTL are pruned by a periodic
    Celery task. After expiry, the same key can be reused for a new
    request.
    """
    request_hash = fingerprint(request_payload)

    # Reservation step in its own short transaction. We commit immediately
    # so a sibling request can SEE this row and refuse, instead of blocking
    # behind us for the full duration of the payout creation.
    try:
        with transaction.atomic():
            ik = IdempotencyKey.objects.create(
                merchant_id=merchant_id,
                key=key,
                request_fingerprint=request_hash,
            )
        reserved_now = True
    except IntegrityError:
        reserved_now = False

    if not reserved_now:
        ik = IdempotencyKey.objects.get(merchant_id=merchant_id, key=key)
        if not _is_within_ttl(ik):
            # Stale row from a previous request that never got cleaned up.
            # Treat as fresh: delete and recurse once.
            ik.delete()
            return idempotent(
                merchant_id=merchant_id,
                key=key,
                request_payload=request_payload,
                handler=handler,
            )

        if ik.request_fingerprint != request_hash:
            raise IdempotencyConflict(
                "idempotency key reused with a different payload",
                key=key,
            )

        if ik.is_complete:
            return ik.response_status, ik.response_body  # type: ignore[return-value]

        # Same key, same payload, but the original request is still in flight.
        # We refuse rather than block, so the client can decide what to do.
        raise IdempotencyInFlight(
            "request with this idempotency key is still being processed",
            key=key,
        )

    # We are the first writer. Run the handler.
    try:
        http_status, body, payout = handler()
    except Exception:
        # Don't leave a half-baked reservation row around — that would block
        # the client from retrying with the same key.
        IdempotencyKey.objects.filter(pk=ik.pk).delete()
        raise

    # Persist the response so future replays return the same bytes.
    IdempotencyKey.objects.filter(pk=ik.pk).update(
        response_status=http_status,
        response_body=body,
        payout=payout,
        completed_at=timezone.now(),
    )
    return http_status, body


def _is_within_ttl(ik: IdempotencyKey) -> bool:
    return timezone.now() - ik.created_at < settings.IDEMPOTENCY_KEY_TTL
