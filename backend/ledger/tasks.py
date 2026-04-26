"""Celery tasks: the worker side of the payout pipeline.

Two tasks live here:

    process_payout(payout_id)
        Picks up a payout in `pending` state, simulates a bank settlement,
        and finalises it as `completed` or `failed`. May leave the payout
        in `processing` (the simulated "hang" outcome) for the reaper to
        re-attempt.

    reap_stuck_payouts()
        Periodic sweep that finds `processing` payouts older than the
        configured threshold and re-queues them with exponential backoff,
        or fails them after MAX_ATTEMPTS.

The retry policy lives in `reap_stuck_payouts` rather than inside
`process_payout` because we want retries to survive a worker crash. If the
worker died mid-task, the payout would still be `processing` in the DB and
the next reap cycle picks it up. Putting retry inside the task body would
lose state when the worker process disappears.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import services
from .exceptions import InvalidTransition
from .models import IdempotencyKey, Payout

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bank settlement simulator
# ---------------------------------------------------------------------------

# Spec: 70% succeed, 20% fail, 10% hang in processing.
_OUTCOMES = (
    ["success"] * 70
    + ["failure"] * 20
    + ["hang"] * 10
)


def _simulate_bank_settlement() -> str:
    """Simulate calling a bank and waiting for the result.

    Real bank rails take 1-30 seconds to ack. We sleep briefly so the worker
    actually exercises concurrency rather than blasting through everything
    serially in microseconds.
    """
    time.sleep(random.uniform(0.2, 1.5))
    return random.choice(_OUTCOMES)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@shared_task(bind=True, max_retries=0)
def process_payout(self, payout_id: str) -> str:
    """Process a single payout end-to-end.

    Two transactions, not one:
        1. Lock the payout row, transition to `processing`.
        2. Release the lock, do the slow bank call (no DB locks held).
        3. Lock the payout row again, transition to `completed` or `failed`.

    Holding a row lock during a slow external call is one of the most common
    payment-system bugs. Don't do it: it blocks the reaper, blocks the API
    if it ever needs the same row, and chains lock waits across requests.
    """
    payout = services.begin_processing(payout_id)
    if payout is None:
        # Already picked up by another worker, or already terminal. Nothing
        # to do. This is normal under concurrent worker scale-out.
        log.info("process_payout: %s no longer pending, skipping", payout_id)
        return "skipped"

    outcome = _simulate_bank_settlement()
    log.info("process_payout: %s simulated outcome=%s", payout_id, outcome)

    if outcome == "hang":
        # Leave the row in `processing`. The reaper will pick it up after
        # PAYOUT_STUCK_AFTER_SECONDS.
        return "hung"

    try:
        if outcome == "success":
            services.mark_completed(payout_id)
            return "completed"
        services.mark_failed(payout_id, reason="Simulated bank settlement failure")
        return "failed"
    except InvalidTransition:
        # Another worker raced us to the terminal state. Safe to ignore.
        log.warning("process_payout: %s already terminal", payout_id)
        return "raced"


# ---------------------------------------------------------------------------
# Stuck-payout reaper
# ---------------------------------------------------------------------------


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff: 1s, 2s, 4s. Cap defensively."""
    return min(2 ** max(0, attempts - 1), 60)


@shared_task
def reap_stuck_payouts() -> int:
    """Find payouts stuck in `processing` and either retry or fail them.

    Runs every 10s via celery beat. Each call is independent — we just
    snapshot the IDs and dispatch follow-up tasks. Per-payout decisions
    happen in `_retry_or_fail` under a row lock so two concurrent reapers
    can't both decide to retry the same row.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.PAYOUT_STUCK_AFTER_SECONDS)
    stuck_ids = list(
        Payout.objects.filter(
            status=Payout.Status.PROCESSING,
            processing_started_at__lt=cutoff,
        )
        .values_list("id", flat=True)
    )
    for pid in stuck_ids:
        _retry_or_fail.delay(str(pid))
    return len(stuck_ids)


@shared_task
def _retry_or_fail(payout_id: str) -> str:
    """Decide whether a stuck payout deserves another attempt.

    Locked decision so two reapers running in parallel can't both decide
    differently. If we've exhausted attempts, mark failed (which releases
    the hold by virtue of leaving `pending`/`processing`). Otherwise reset
    the timer and re-dispatch the worker after a backoff delay.
    """
    with transaction.atomic():
        try:
            payout = Payout.objects.select_for_update().get(pk=payout_id)
        except Payout.DoesNotExist:
            return "missing"

        if payout.status != Payout.Status.PROCESSING:
            # Already resolved between the reaper sweep and now.
            return "no-op"

        if payout.attempts >= settings.PAYOUT_MAX_ATTEMPTS:
            payout.transition_to(Payout.Status.FAILED)
            payout.failure_reason = "Max retries exceeded"
            payout.save(update_fields=["status", "failure_reason", "updated_at"])
            return "failed-after-retries"

        # Still under the cap. Re-arm and re-dispatch.
        services.restart_processing(payout_id)
        attempts = payout.attempts + 1

    process_payout.apply_async(
        args=[payout_id], countdown=_backoff_seconds(attempts)
    )
    return f"requeued (attempt={attempts})"


# ---------------------------------------------------------------------------
# Idempotency key TTL pruner
# ---------------------------------------------------------------------------


@shared_task
def purge_expired_idempotency_keys() -> int:
    """Drop idempotency rows older than the TTL so keys can be reused."""
    cutoff = timezone.now() - settings.IDEMPOTENCY_KEY_TTL
    deleted, _ = IdempotencyKey.objects.filter(created_at__lt=cutoff).delete()
    return deleted
