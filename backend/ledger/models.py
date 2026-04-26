"""Ledger and payout models.

Design choices (see EXPLAINER.md for the long version):

1. Money is stored as `BigIntegerField` paise. No floats, no Decimal. One
   integer column, signed math, no surprises from rounding.

2. The ledger is append-only. Every credit and every debit is one row in
   `LedgerEntry` with `kind ∈ {CREDIT, DEBIT}` and a positive amount. The sign
   is encoded in the `kind`, never in the amount.

3. Holds for in-flight payouts are NOT separate ledger rows. They are derived
   from the `Payout` table itself: any payout with status `pending` or
   `processing` is a hold against the merchant's balance. This keeps the
   ledger purely a record of settled money movement, and there's exactly one
   place to update when a hold resolves (the payout row's status).

The invariant we enforce everywhere:

    available_balance(merchant)
      = SUM(CREDIT.amount) - SUM(DEBIT.amount) - SUM(active_holds.amount)
"""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import CheckConstraint, Q, UniqueConstraint

from .exceptions import InvalidTransition


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Merchant(TimestampedModel):
    """A merchant collecting USD payments and withdrawing INR.

    Why a dedicated row even though we don't store balance on it? Because the
    row is the per-merchant mutex: every balance-mutating operation begins
    with `SELECT FOR UPDATE` on this row. See `services.lock_merchant`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class BankAccount(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.CASCADE, related_name="bank_accounts"
    )
    account_holder = models.CharField(max_length=200)
    account_number_last4 = models.CharField(max_length=4)
    ifsc = models.CharField(max_length=11)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.account_holder} ****{self.account_number_last4}"


class LedgerEntry(TimestampedModel):
    """A single, immutable money-movement event for a merchant.

    Append-only — never updated, never deleted. Balance is always recomputed
    from this table. That gives us trivial auditability: replay the rows in
    `created_at` order and you have a perfect history.
    """

    class Kind(models.TextChoices):
        CREDIT = "CREDIT", "Credit"
        DEBIT = "DEBIT", "Debit"

    id = models.BigAutoField(primary_key=True)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    kind = models.CharField(max_length=8, choices=Kind.choices)
    # Always positive; sign comes from `kind`.
    amount_paise = models.BigIntegerField()
    description = models.CharField(max_length=255, blank=True)
    # If this debit is the settlement of a payout, link it. CREDITs never link
    # to payouts (in this MVP they come from simulated customer payments).
    payout = models.ForeignKey(
        "Payout",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )

    class Meta:
        indexes = [models.Index(fields=["merchant", "created_at"])]
        constraints = [
            CheckConstraint(
                check=Q(amount_paise__gt=0),
                name="ledger_entry_amount_positive",
            ),
        ]


class Payout(TimestampedModel):
    """A merchant withdrawal to an Indian bank account.

    The state machine: pending -> processing -> {completed | failed}. Anything
    backwards or sideways is rejected by `transition_to`. `completed` and
    `failed` are terminal.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    LEGAL_TRANSITIONS = {
        Status.PENDING: {Status.PROCESSING},
        Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
        Status.COMPLETED: set(),  # terminal
        Status.FAILED: set(),  # terminal
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name="payouts"
    )
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="payouts"
    )
    amount_paise = models.BigIntegerField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [
            # Used by the worker scheduler and the stuck-payout reaper.
            models.Index(fields=["status", "processing_started_at"]),
            models.Index(fields=["merchant", "created_at"]),
        ]
        constraints = [
            CheckConstraint(
                check=Q(amount_paise__gt=0),
                name="payout_amount_positive",
            ),
        ]

    # --- State machine -------------------------------------------------------

    def transition_to(self, new_status: "Payout.Status") -> None:
        """Move to `new_status` or raise InvalidTransition.

        The check is purely table-driven (`LEGAL_TRANSITIONS`). Any move not
        listed is rejected — including failed->completed, completed->pending,
        and all backwards/sideways paths. The caller is responsible for doing
        this inside a transaction along with whatever side effects the move
        implies (e.g. inserting a DEBIT ledger entry on completion).
        """
        allowed = self.LEGAL_TRANSITIONS[self.Status(self.status)]
        if Payout.Status(new_status) not in allowed:
            raise InvalidTransition(
                f"illegal transition {self.status} -> {new_status}",
                payout_id=str(self.id),
                from_status=self.status,
                to_status=new_status,
            )
        self.status = new_status

    @property
    def is_held(self) -> bool:
        return self.status in {self.Status.PENDING, self.Status.PROCESSING}

    @property
    def is_terminal(self) -> bool:
        return self.status in {self.Status.COMPLETED, self.Status.FAILED}


class IdempotencyKey(TimestampedModel):
    """Records merchant-supplied keys we have already seen.

    The `(merchant, key)` UNIQUE constraint is the serialization point: when
    two requests arrive simultaneously with the same key, exactly one INSERT
    succeeds. The other gets IntegrityError and reads the existing row. See
    `services.idempotent` for the full flow.
    """

    id = models.BigAutoField(primary_key=True)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.CASCADE, related_name="idempotency_keys"
    )
    key = models.CharField(max_length=128)
    # SHA-256 of the canonical request body. Lets us detect when a client
    # reuses a key with a different payload (a real bug we should refuse).
    request_fingerprint = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    payout = models.ForeignKey(
        Payout,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="idempotency_keys",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["merchant", "key"], name="idempotency_unique_per_merchant"
            ),
        ]
        indexes = [models.Index(fields=["created_at"])]

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None
