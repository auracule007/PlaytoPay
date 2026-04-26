"""HTTP handlers (Class-Based Views).

Pattern: each view validates input, looks up the merchant context, then
delegates to `services.py`. No DB writes happen in this file. That keeps
the locking and idempotency logic in one place where it can be reasoned
about as a unit.

We use DRF's `APIView` (Class-Based Views) so each endpoint reads as a
small, focused class with explicit `get`/`post` methods. A shared
`MerchantContextMixin` resolves the calling merchant from the
`X-Merchant-Id` header so we don't repeat that boilerplate in every view.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import LedgerError
from .models import BankAccount, LedgerEntry, Merchant, Payout
from .serializers import (
    BalanceSerializer,
    BankAccountSerializer,
    LedgerEntrySerializer,
    MerchantSerializer,
    PayoutCreateSerializer,
    PayoutSerializer,
)
from . import services
from .tasks import process_payout


# ---------------------------------------------------------------------------
# Mixin: resolve the calling merchant from headers
# ---------------------------------------------------------------------------


class MerchantContextMixin:
    """Pulls `X-Merchant-Id` off the request and resolves the Merchant row.

    Real production would replace this with API-key-to-merchant middleware.
    Keeping the lookup in one place makes that swap a one-file change.
    """

    def get_merchant(self, request) -> Merchant:
        merchant_id = request.headers.get("X-Merchant-Id")
        if not merchant_id:
            raise LedgerError("X-Merchant-Id header required")
        try:
            return Merchant.objects.get(pk=merchant_id)
        except (Merchant.DoesNotExist, ValueError) as e:
            # ValueError covers a malformed UUID being passed as the header
            # value; we treat both as "no such merchant".
            raise LedgerError("merchant not found") from e


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


class MerchantListView(APIView):
    """GET /api/v1/merchants — convenience helper for the demo UI.

    A real product would not expose this; the merchant is implied by their
    own API key. We expose it so the dashboard can show a picker.
    """

    def get(self, request):
        qs = Merchant.objects.all().order_by("name")
        return Response(MerchantSerializer(qs, many=True).data)


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


class BalanceView(MerchantContextMixin, APIView):
    """GET /api/v1/balance — settled, held, and available balance in paise."""

    def get(self, request):
        merchant = self.get_merchant(request)
        balance = services.compute_balance(merchant.id)
        body = BalanceSerializer(
            {
                "settled_paise": balance.settled,
                "held_paise": balance.held,
                "available_paise": balance.available,
            }
        ).data
        return Response(body)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class LedgerEntryListView(MerchantContextMixin, APIView):
    """GET /api/v1/ledger — last 50 credit/debit rows, newest first."""

    def get(self, request):
        merchant = self.get_merchant(request)
        qs = (
            LedgerEntry.objects.filter(merchant=merchant)
            .order_by("-created_at")[:50]
        )
        return Response(LedgerEntrySerializer(qs, many=True).data)


# ---------------------------------------------------------------------------
# Bank accounts
# ---------------------------------------------------------------------------


class BankAccountListView(MerchantContextMixin, APIView):
    """GET /api/v1/bank-accounts — destinations the merchant can pay out to."""

    def get(self, request):
        merchant = self.get_merchant(request)
        qs = merchant.bank_accounts.all().order_by("created_at")
        return Response(BankAccountSerializer(qs, many=True).data)


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------


class PayoutCollectionView(MerchantContextMixin, APIView):
    """GET /api/v1/payouts (list) and POST /api/v1/payouts (create).

    Splitting list/create across HTTP methods on the same URL keeps the
    public surface clean. The two methods share no logic; each delegates
    straight to either an ORM read or the `services.idempotent` wrapper.
    """

    def get(self, request):
        merchant = self.get_merchant(request)
        qs = (
            Payout.objects.filter(merchant=merchant)
            .order_by("-created_at")[:50]
        )
        return Response(PayoutSerializer(qs, many=True).data)

    def post(self, request):
        """Create a payout.

        Headers:
            X-Merchant-Id: UUID of the calling merchant.
            Idempotency-Key: client-supplied UUID (required).

        Body:
            {"amount_paise": 12345, "bank_account_id": "<uuid>"}
        """
        merchant = self.get_merchant(request)

        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            raise LedgerError("Idempotency-Key header required")

        serializer = PayoutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        # Verify the bank account belongs to this merchant before we burn an
        # idempotency key on a doomed request.
        if not BankAccount.objects.filter(
            pk=payload["bank_account_id"], merchant=merchant
        ).exists():
            raise LedgerError("bank_account_id not found for this merchant")

        def _run() -> tuple[int, dict, Payout]:
            payout = services.create_payout(
                merchant_id=merchant.id,
                bank_account_id=payload["bank_account_id"],
                amount_paise=payload["amount_paise"],
            )
            # Schedule the worker AFTER the transaction in
            # services.create_payout has committed. Otherwise the worker
            # could race ahead and not see the payout row yet.
            # `services.create_payout` is `@transaction.atomic`, so by the
            # time we're here it has already committed.
            process_payout.delay(str(payout.id))
            body = PayoutSerializer(payout).data
            return status.HTTP_201_CREATED, body, payout

        http_status, body = services.idempotent(
            merchant_id=merchant.id,
            key=idempotency_key,
            request_payload={
                "amount_paise": payload["amount_paise"],
                "bank_account_id": str(payload["bank_account_id"]),
            },
            handler=_run,
        )
        return Response(body, status=http_status)


class PayoutDetailView(MerchantContextMixin, APIView):
    """GET /api/v1/payouts/<uuid> — single payout, scoped to the merchant."""

    def get(self, request, payout_id):
        merchant = self.get_merchant(request)
        try:
            payout = Payout.objects.get(pk=payout_id, merchant=merchant)
        except Payout.DoesNotExist:
            return Response(
                {"error": {"code": "not_found", "message": "payout not found"}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(PayoutSerializer(payout).data)
