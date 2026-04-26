"""Idempotent seed script: `python manage.py seed`.

Creates 3 demo merchants, each with a bank account and a handful of CREDIT
ledger entries (simulated customer payments). Re-running the script wipes
the prior demo data and reseeds, so reviewers can put the database in a
known state at any time.

Why wipe-and-reseed instead of get_or_create? Because demo data drifts:
old payouts pile up, balances get weird, and "is the bug in my code or in
the seed data" becomes a real question. A clean slate every time removes
that ambiguity.
"""
from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand
from django.db import transaction

from ledger.models import (
    BankAccount,
    IdempotencyKey,
    LedgerEntry,
    Merchant,
    Payout,
)


SEED_MERCHANTS = [
    {
        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "name": "Studio Bombay",
        "email": "studio@bombay.demo",
        "bank_account_id": uuid.UUID("aaaaaaaa-1111-1111-1111-111111111111"),
        "account_holder": "Studio Bombay LLP",
        "account_number_last4": "4321",
        "ifsc": "HDFC0001234",
        "credits_paise": [
            (50_000_00, "Customer payment - Acme Corp"),
            (75_000_00, "Customer payment - Globex"),
            (12_500_00, "Customer payment - Initech"),
        ],
    },
    {
        "id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "name": "Pixel Forge Agency",
        "email": "ops@pixelforge.demo",
        "bank_account_id": uuid.UUID("aaaaaaaa-2222-2222-2222-222222222222"),
        "account_holder": "Pixel Forge Pvt Ltd",
        "account_number_last4": "9876",
        "ifsc": "ICIC0009876",
        "credits_paise": [
            (1_20_000_00, "Customer payment - Hooli"),
            (45_000_00, "Customer payment - Pied Piper"),
        ],
    },
    {
        "id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "name": "Mira Freelance",
        "email": "mira@freelance.demo",
        "bank_account_id": uuid.UUID("aaaaaaaa-3333-3333-3333-333333333333"),
        "account_holder": "Mira Iyer",
        "account_number_last4": "0042",
        "ifsc": "AXIS0001111",
        "credits_paise": [
            (30_000_00, "Customer payment - Wonka Industries"),
            (8_500_00, "Customer payment - Stark Industries"),
        ],
    },
]


class Command(BaseCommand):
    help = "Seed demo merchants, bank accounts, and credit history."

    @transaction.atomic
    def handle(self, *args, **options):
        # Clean slate. Order matters: payouts and ledger reference merchants.
        IdempotencyKey.objects.all().delete()
        LedgerEntry.objects.all().delete()
        Payout.objects.all().delete()
        BankAccount.objects.all().delete()
        Merchant.objects.all().delete()

        for spec in SEED_MERCHANTS:
            merchant = Merchant.objects.create(
                id=spec["id"], name=spec["name"], email=spec["email"]
            )
            BankAccount.objects.create(
                id=spec["bank_account_id"],
                merchant=merchant,
                account_holder=spec["account_holder"],
                account_number_last4=spec["account_number_last4"],
                ifsc=spec["ifsc"],
            )
            for amount_paise, description in spec["credits_paise"]:
                LedgerEntry.objects.create(
                    merchant=merchant,
                    kind=LedgerEntry.Kind.CREDIT,
                    amount_paise=amount_paise,
                    description=description,
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  seeded {merchant.name} ({merchant.id}) - "
                    f"{len(spec['credits_paise'])} credits"
                )
            )

        self.stdout.write(self.style.SUCCESS("Seed complete."))
