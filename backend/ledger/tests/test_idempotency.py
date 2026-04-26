"""Idempotency: same key twice -> same response, exactly one payout.

Two scenarios are covered here:

    1. Sequential replay (request finished, client retries with same key).
       Must return the cached response and create no new payout.

    2. Same key with a different payload (client bug).
       Must be rejected with idempotency_conflict, not silently accepted.

We exercise the actual HTTP path via Django's test client to make sure the
view + service + table-constraint chain all line up. A unit test on
services.idempotent alone wouldn't catch a missing header read in views.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

from django.test import TestCase

from ledger.models import (
    BankAccount,
    IdempotencyKey,
    LedgerEntry,
    Merchant,
    Payout,
)


class IdempotencyTests(TestCase):
    def setUp(self) -> None:
        # The view dispatches the worker via Celery .delay(). We don't have a
        # broker in tests and we don't care about the worker for these
        # assertions, so swap it for a no-op for the duration of each test.
        patcher = patch("ledger.views.process_payout.delay")
        self.delay_mock = patcher.start()
        self.addCleanup(patcher.stop)

        self.merchant = Merchant.objects.create(
            name="Idem Co", email=f"idem-{uuid.uuid4()}@demo"
        )
        self.bank = BankAccount.objects.create(
            merchant=self.merchant,
            account_holder="Idem Co",
            account_number_last4="9999",
            ifsc="ICIC0000099",
        )
        LedgerEntry.objects.create(
            merchant=self.merchant,
            kind=LedgerEntry.Kind.CREDIT,
            amount_paise=10_000_00,
        )

    def _post_payout(self, key: str, body: dict) -> tuple[int, dict]:
        resp = self.client.post(
            "/api/v1/payouts",
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_MERCHANT_ID=str(self.merchant.id),
            HTTP_IDEMPOTENCY_KEY=key,
        )
        return resp.status_code, resp.json()

    def test_replay_returns_cached_response(self) -> None:
        key = str(uuid.uuid4())
        body = {"amount_paise": 500_00, "bank_account_id": str(self.bank.id)}

        status_a, body_a = self._post_payout(key, body)
        status_b, body_b = self._post_payout(key, body)

        self.assertEqual(status_a, 201)
        self.assertEqual(status_b, 201)
        # Exact same response — same payout id, same created_at, etc.
        self.assertEqual(body_a, body_b)
        # Exactly one Payout row was created.
        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 1)
        # Exactly one IdempotencyKey row, with completed_at set.
        self.assertEqual(IdempotencyKey.objects.filter(merchant=self.merchant).count(), 1)
        ik = IdempotencyKey.objects.get(merchant=self.merchant)
        self.assertIsNotNone(ik.completed_at)

    def test_same_key_different_payload_is_rejected(self) -> None:
        key = str(uuid.uuid4())
        body_a = {"amount_paise": 500_00, "bank_account_id": str(self.bank.id)}
        body_b = {"amount_paise": 600_00, "bank_account_id": str(self.bank.id)}

        status_a, _ = self._post_payout(key, body_a)
        status_b, body_resp = self._post_payout(key, body_b)

        self.assertEqual(status_a, 201)
        # Server must refuse, not silently dispatch the second amount.
        self.assertEqual(status_b, 422)
        self.assertEqual(body_resp["error"]["code"], "idempotency_conflict")
        # Still only one payout row.
        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 1)
