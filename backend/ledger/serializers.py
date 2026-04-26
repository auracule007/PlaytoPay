"""DRF serializers.

We keep these dumb on purpose: input validation only, no DB access, no
business rules. All side-effecting logic lives in `services.py` so it can
be tested without HTTP plumbing.
"""
from rest_framework import serializers

from .models import BankAccount, LedgerEntry, Merchant, Payout


class MerchantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Merchant
        fields = ["id", "name", "email", "created_at"]


class BankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankAccount
        fields = ["id", "account_holder", "account_number_last4", "ifsc"]


class LedgerEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LedgerEntry
        fields = [
            "id",
            "kind",
            "amount_paise",
            "description",
            "payout",
            "created_at",
        ]


class PayoutSerializer(serializers.ModelSerializer):
    bank_account = BankAccountSerializer(read_only=True)

    class Meta:
        model = Payout
        fields = [
            "id",
            "amount_paise",
            "status",
            "attempts",
            "bank_account",
            "processing_started_at",
            "completed_at",
            "failure_reason",
            "created_at",
            "updated_at",
        ]


class PayoutCreateSerializer(serializers.Serializer):
    """Request body for POST /api/v1/payouts."""

    amount_paise = serializers.IntegerField(min_value=1)
    bank_account_id = serializers.UUIDField()

    def validate_amount_paise(self, value: int) -> int:
        # We never want to see a float sneaking through the boundary. DRF's
        # IntegerField already coerces, but we're paranoid.
        if not isinstance(value, int) or isinstance(value, bool):
            raise serializers.ValidationError("amount_paise must be an integer")
        return value


class BalanceSerializer(serializers.Serializer):
    settled_paise = serializers.IntegerField()
    held_paise = serializers.IntegerField()
    available_paise = serializers.IntegerField()
