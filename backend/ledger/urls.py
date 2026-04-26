"""URL routing for the ledger app.

All endpoints live under `/api/v1/` (see project-level urls.py). Routes
hand off to Class-Based Views in `views.py`. Keeping this file thin makes
the URL surface easy to scan in code review.
"""
from django.urls import path

from .views import (
    BalanceView,
    BankAccountListView,
    LedgerEntryListView,
    MerchantListView,
    PayoutCollectionView,
    PayoutDetailView,
)

urlpatterns = [
    path("merchants", MerchantListView.as_view(), name="merchants"),
    path("balance", BalanceView.as_view(), name="balance"),
    path("ledger", LedgerEntryListView.as_view(), name="ledger"),
    path("bank-accounts", BankAccountListView.as_view(), name="bank-accounts"),
    # GET = list, POST = create. Same URL on purpose — the spec uses it.
    path("payouts", PayoutCollectionView.as_view(), name="payouts"),
    path(
        "payouts/<uuid:payout_id>",
        PayoutDetailView.as_view(),
        name="payout-detail",
    ),
]
