"""Read-only admin surfaces for inspection during demo / debugging.

We deliberately do NOT register editable forms for LedgerEntry or Payout —
mutating them via Django admin would bypass the locking and state-machine
guards. If you need to fix data, write a service function with the lock.
"""
from django.contrib import admin

from .models import BankAccount, IdempotencyKey, LedgerEntry, Merchant, Payout


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "id", "created_at")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("account_holder", "merchant", "account_number_last4", "ifsc")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "kind", "amount_paise", "created_at")
    list_filter = ("kind",)
    readonly_fields = tuple(
        f.name for f in LedgerEntry._meta.get_fields() if hasattr(f, "name")
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "amount_paise", "status", "attempts", "created_at")
    list_filter = ("status",)
    readonly_fields = tuple(
        f.name for f in Payout._meta.get_fields() if hasattr(f, "name")
    )

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ("merchant", "key", "response_status", "completed_at", "created_at")
