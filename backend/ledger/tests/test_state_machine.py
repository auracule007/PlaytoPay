"""Tests for the payout state machine.

These are unit tests against the model method directly — fast, no DB
required for the rejection paths. The legal paths still hit the DB so we
can confirm the row updates atomically with the field changes.
"""
from __future__ import annotations

import uuid

from django.test import TestCase

from ledger.exceptions import InvalidTransition
from ledger.models import BankAccount, Merchant, Payout


class StateMachineTests(TestCase):
    def setUp(self) -> None:
        self.merchant = Merchant.objects.create(
            name="SM", email=f"sm-{uuid.uuid4()}@demo"
        )
        self.bank = BankAccount.objects.create(
            merchant=self.merchant,
            account_holder="SM",
            account_number_last4="0000",
            ifsc="HDFC0000001",
        )

    def _new_payout(self, status=Payout.Status.PENDING) -> Payout:
        return Payout.objects.create(
            merchant=self.merchant,
            bank_account=self.bank,
            amount_paise=100,
            status=status,
        )

    def test_pending_to_processing_allowed(self) -> None:
        p = self._new_payout()
        p.transition_to(Payout.Status.PROCESSING)
        self.assertEqual(p.status, Payout.Status.PROCESSING)

    def test_processing_to_completed_allowed(self) -> None:
        p = self._new_payout(Payout.Status.PROCESSING)
        p.transition_to(Payout.Status.COMPLETED)
        self.assertEqual(p.status, Payout.Status.COMPLETED)

    def test_processing_to_failed_allowed(self) -> None:
        p = self._new_payout(Payout.Status.PROCESSING)
        p.transition_to(Payout.Status.FAILED)
        self.assertEqual(p.status, Payout.Status.FAILED)

    def test_failed_to_completed_rejected(self) -> None:
        """The headline forbidden transition from the rubric."""
        p = self._new_payout(Payout.Status.FAILED)
        with self.assertRaises(InvalidTransition):
            p.transition_to(Payout.Status.COMPLETED)

    def test_completed_is_terminal(self) -> None:
        p = self._new_payout(Payout.Status.COMPLETED)
        for target in (
            Payout.Status.PENDING,
            Payout.Status.PROCESSING,
            Payout.Status.FAILED,
        ):
            with self.assertRaises(InvalidTransition):
                p.transition_to(target)

    def test_pending_to_terminal_rejected(self) -> None:
        """You cannot skip the processing step."""
        p = self._new_payout()
        with self.assertRaises(InvalidTransition):
            p.transition_to(Payout.Status.COMPLETED)
        with self.assertRaises(InvalidTransition):
            p.transition_to(Payout.Status.FAILED)

    def test_backwards_rejected(self) -> None:
        p = self._new_payout(Payout.Status.PROCESSING)
        with self.assertRaises(InvalidTransition):
            p.transition_to(Payout.Status.PENDING)
