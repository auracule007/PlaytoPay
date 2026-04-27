# EXPLAINER

Every code snippet below is copied directly from the running codebase.

---

## 1. The Ledger

### Balance calculation query

```python
# backend/ledger/services.py

def compute_balance(merchant_id) -> Balance:
    settled = (
        LedgerEntry.objects.filter(merchant_id=merchant_id)
        .aggregate(
            credits=Sum("amount_paise", filter=Q(kind=LedgerEntry.Kind.CREDIT)),
            debits=Sum("amount_paise", filter=Q(kind=LedgerEntry.Kind.DEBIT)),
        )
    )
    credits = settled["credits"] or 0
    debits  = settled["debits"]  or 0

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
```

That ORM call compiles to roughly this SQL:

```sql
-- Ledger aggregation
SELECT
  COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'CREDIT'), 0) AS credits,
  COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'DEBIT'),  0) AS debits
FROM ledger_ledgerentry
WHERE merchant_id = %s;

-- Hold aggregation
SELECT COALESCE(SUM(amount_paise), 0) AS held
FROM ledger_payout
WHERE merchant_id = %s
  AND status IN ('pending', 'processing');
```

`available = (credits − debits) − held`

### Why this ledger model

I chose a single `LedgerEntry` table with `kind ∈ {CREDIT, DEBIT}`, amounts always positive, sign encoded in the kind. Holds are **implicit** — a `Payout` in `pending` or `processing` state is the hold; there is no separate HOLD/RELEASE row.

Three reasons this shape won out over alternatives (four-event ledger, double-entry, stored balance):

1. **The ledger stays append-only.** We never UPDATE or DELETE rows in `LedgerEntry`. Replaying rows in `created_at` order recovers exact history. There are no compensating entries when a payout fails because nothing was written for the hold in the first place.

2. **One source of truth for the hold.** The hold lives in `Payout.status`. Releasing it is a single `UPDATE` on one row. With a separate HOLD/RELEASE table you have two writes per release that must stay in lockstep — any drift is silent corruption.

3. **The vocabulary matches the spec.** Credits and debits are the language of the problem. Sticking to it makes the model legible to anyone reading it cold.

**Trade-off:** balance reads do two aggregations instead of one. Both run under indexes (`merchant_id, created_at` on ledger; a partial-range scan on payouts by `merchant_id` and `status`). If that becomes a bottleneck at scale, materializing balance into a `merchant_balance` row updated transactionally is a known, safe refactor.

### Why integer paise, not Decimal

`BigIntegerField` paise gives `2^63 ≈ 9.2 × 10^18` paise of headroom. Floats are disqualified — `0.1 + 0.2 ≠ 0.3` is not a thought experiment, it is the reality of IEEE 754. `DecimalField` would also work but introduces a second memory model: every engineer who touches the code needs to know which operations preserve precision and which silently coerce. With integers there is nothing to remember.

---

## 2. The Lock

### The exact code that prevents concurrent overdrafts

```python
# backend/ledger/services.py

def lock_merchant(merchant_id) -> Merchant:
    """Acquire a row-level exclusive lock on the merchant row.

    Must be called inside a transaction. Postgres blocks any other transaction
    that calls this on the same merchant until we commit.
    """
    return Merchant.objects.select_for_update().get(pk=merchant_id)


@transaction.atomic
def create_payout(*, merchant_id, bank_account_id, amount_paise: int) -> Payout:
    if amount_paise <= 0:
        raise LedgerError("amount must be positive")

    merchant = lock_merchant(merchant_id)   # <-- the lock, BEFORE the balance read
    balance  = compute_balance(merchant.id) # <-- balance read is inside the locked window

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
```

### What database primitive this relies on

`select_for_update()` emits `SELECT … FOR UPDATE` in Postgres. That acquires a **row-level exclusive lock** on the merchant row. Any other transaction that attempts the same `SELECT … FOR UPDATE` on the same row blocks until the first transaction commits or rolls back. Regular reads (without `FOR UPDATE`) are not blocked.

This defeats the classic check-then-act race:

```
Without the lock:
  T1: read balance → 100
  T2: read balance → 100   (sees the same snapshot)
  T1: INSERT payout 60    → balance now 40
  T2: INSERT payout 60    → balance now −20  ✗

With the lock:
  T1: SELECT … FOR UPDATE on merchant row (lock acquired)
  T1: read balance → 100
  T2: SELECT … FOR UPDATE on merchant row (blocks here)
  T1: INSERT payout 60; COMMIT             (lock released)
  T2: unblocked; read balance → 40
  T2: 40 < 60 → InsufficientBalance        ✓
```

**Why lock the merchant row and not a balance row or the payouts table?**
The invariant is per-merchant — two payouts from different merchants are completely independent and must not serialize against each other. The merchant row is the natural per-merchant mutex. There is no balance row to lock (we deliberately do not store one), and locking "all payouts for this merchant" would require `FOR UPDATE` over a result set, which is noisier for no gain.

**One deliberate design point:** the worker never holds a DB lock during the slow bank-call simulation. It transitions `pending → processing` under a lock, releases, sleeps, then re-acquires the payout row lock to write the terminal state. Holding a lock through a 30-second external call would block the API entirely for that merchant.

The test that proves this works is `test_two_simultaneous_payouts_for_more_than_balance` in `backend/ledger/tests/test_concurrency.py`. It uses `TransactionTestCase` with real commits between threads (not Django's test-transaction magic) and `threading.Barrier` to fire both threads simultaneously. One always succeeds; the other always raises `InsufficientBalance`.

---

## 3. The Idempotency

### How the system recognises a key it has seen before

The `IdempotencyKey` table has a `UNIQUE(merchant_id, key)` constraint. That constraint is the serialization point — Postgres guarantees at most one row exists per `(merchant, key)` regardless of how many concurrent INSERTs race for it.

```python
# backend/ledger/services.py  (condensed for clarity)

request_hash = fingerprint(request_payload)   # sha256(canonical_json)

try:
    with transaction.atomic():
        ik = IdempotencyKey.objects.create(
            merchant_id=merchant_id,
            key=key,
            request_fingerprint=request_hash,
        )
    reserved_now = True
except IntegrityError:                 # unique constraint violation
    reserved_now = False

if reserved_now:
    # We won the race — run the actual handler.
    try:
        http_status, body, payout = handler()
    except Exception:
        IdempotencyKey.objects.filter(pk=ik.pk).delete()  # clear reservation on failure
        raise
    IdempotencyKey.objects.filter(pk=ik.pk).update(
        response_status=http_status,
        response_body=body,
        payout=payout,
        completed_at=timezone.now(),
    )
    return http_status, body

# We lost the race — read the existing row.
ik = IdempotencyKey.objects.get(merchant_id=merchant_id, key=key)

if ik.request_fingerprint != request_hash:
    raise IdempotencyConflict("idempotency key reused with a different payload")

if ik.is_complete:
    return ik.response_status, ik.response_body   # cached response, byte-for-byte

raise IdempotencyInFlight("request with this key is still being processed")
```

The `request_fingerprint` is `sha256(canonical_json(payload))` — sorted keys, no whitespace — so `{"amount_paise":500,"bank_account_id":"..."}` and `{"bank_account_id":"...","amount_paise":500}` produce the same hash. A different payload with the same key gets `422 idempotency_conflict`, which is a genuine client bug we should surface, not silently accept.

### What happens when the second request arrives while the first is still in flight

The reservation INSERT in step 1 commits **before** the handler runs, in its own short transaction. The moment request A has committed its reservation, request B (arriving milliseconds later) gets an `IntegrityError` on its INSERT, reads the existing row, sees `completed_at IS NULL`, and returns `409 IdempotencyInFlight`.

**Why refuse instead of blocking?** Blocking B until A finishes would mean holding B's HTTP connection open for as long as bank settlement takes (potentially seconds). On a multi-instance deployment it also requires advisory locks because B may land on a different web process than A. Refusing fast with a meaningful error code is simpler, predictable, and idempotency-key users (who have retry logic by definition) handle it cleanly — they retry after a short delay and get either the cached `201` or another `409`.

**Key scoping and expiry:**
- Keys are scoped per merchant via the unique constraint on `(merchant_id, key)`. Two merchants can use the same key string without conflict.
- Keys expire after 24 hours via the `purge_expired_idempotency_keys` Celery beat task (runs hourly). After expiry the same key can be reused for a new request.

---

## 4. The State Machine

### Where `failed → completed` is blocked

```python
# backend/ledger/models.py

class Payout(TimestampedModel):
    class Status(models.TextChoices):
        PENDING    = "pending",    "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED  = "completed",  "Completed"
        FAILED     = "failed",     "Failed"

    LEGAL_TRANSITIONS = {
        Status.PENDING:    {Status.PROCESSING},
        Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
        Status.COMPLETED:  set(),   # terminal — no exits
        Status.FAILED:     set(),   # terminal — no exits
    }

    def transition_to(self, new_status: "Payout.Status") -> None:
        allowed = self.LEGAL_TRANSITIONS[self.Status(self.status)]
        if Payout.Status(new_status) not in allowed:
            raise InvalidTransition(
                f"illegal transition {self.status} -> {new_status}",
                payout_id=str(self.id),
                from_status=self.status,
                to_status=new_status,
            )
        self.status = new_status
```

`LEGAL_TRANSITIONS[Status.FAILED]` is the empty set. Any `new_status` fails the `not in allowed` check, because nothing is in an empty set. This blocks `failed → completed`, `failed → pending`, `failed → processing`, and everything else. Same logic blocks `completed → anything`. Backwards moves (`processing → pending`) and skips (`pending → completed`) are blocked for the same reason — they are simply not in the allowed set for their source state.

The check is table-driven. There are no `if/elif` ladders to drift. Adding a new status is two edits: the enum and one new row in the dict.

### Atomicity of state transitions with their side effects

The state guard alone is not enough — side effects must commit with the transition. Two examples:

```python
# services.py — on success, the status flip and the DEBIT entry are one transaction.
@transaction.atomic
def mark_completed(payout_id) -> None:
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    payout.transition_to(Payout.Status.COMPLETED)   # guard enforced here
    payout.completed_at = timezone.now()
    payout.save(update_fields=["status", "completed_at", "updated_at"])
    LedgerEntry.objects.create(                      # debit in same transaction
        merchant=payout.merchant,
        kind=LedgerEntry.Kind.DEBIT,
        amount_paise=payout.amount_paise,
        description=f"Payout {payout.id}",
        payout=payout,
    )
```

Both the status flip and the DEBIT row commit together or not at all. There is no window in which a payout reads "completed" with no corresponding debit.

```python
# services.py — on failure, the hold release IS the status transition.
@transaction.atomic
def mark_failed(payout_id, *, reason: str) -> None:
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    payout.transition_to(Payout.Status.FAILED)
    payout.failure_reason = reason[:255]
    payout.save(update_fields=["status", "failure_reason", "updated_at"])
```

"Releasing the held funds" is a single-row UPDATE that changes `status` to `failed`. No second write. No compensating ledger entry. The hold was always implicit (funds are held because `status ∈ {pending, processing}`), so making the status terminal is the entire release. That atomicity is free.

---

## 5. The AI Audit

Claude was used as a pair programmer throughout this project. The most significant correction was in `create_payout`.

### What AI gave me first

```python
@transaction.atomic
def create_payout(merchant_id, amount_paise, ...):
    # Check balance first
    current_balance = compute_balance(merchant_id)
    if current_balance.available < amount_paise:
        raise InsufficientBalance(...)
    # Then lock and write
    merchant = Merchant.objects.select_for_update().get(pk=merchant_id)
    return Payout.objects.create(merchant=merchant, amount_paise=amount_paise, ...)
```

### What is wrong with it

The balance check happens **before** the lock. Two concurrent transactions both compute `available = 100`, both pass the guard, then both serialize on the `SELECT FOR UPDATE`. At that point the decision has already been made — the lock is doing nothing useful. Both transactions insert their payouts and the balance goes negative. This is the "lock exists but is in the wrong place" failure mode.

### What I replaced it with

```python
@transaction.atomic
def create_payout(*, merchant_id, bank_account_id, amount_paise: int) -> Payout:
    if amount_paise <= 0:
        raise LedgerError("amount must be positive")

    merchant = lock_merchant(merchant_id)            # lock FIRST
    balance  = compute_balance(merchant.id)          # read INSIDE the locked window

    if balance.available < amount_paise:
        raise InsufficientBalance(...)

    return Payout.objects.create(...)
```

Lock, then read, then decide, then write, then commit. The order is not interchangeable.

### Two other things I rejected from AI suggestions

**String interpolation in the aggregation filter.** An early draft built the `Sum(filter=Q(...))` clause using f-strings rather than the Django model constants (`LedgerEntry.Kind.CREDIT`). The model constants are parameterized by the ORM into safe SQL. String interpolation on a money table would be a SQL injection waiting to happen. Replaced with constants.

**`.update()` for the state transition.** Suggested `Payout.objects.filter(id=…).update(status="failed")` instead of fetch → `transition_to` → `save`. `.update()` bypasses the `transition_to` method, which means the `LEGAL_TRANSITIONS` table is never consulted. The state machine guard becomes advisory — it runs on some code paths but not others. Refused; kept the fetch-and-save pattern throughout, even though it costs one extra round trip. The guard is the whole point.

### Pattern that worked

AI for shape and initial structure, careful re-reading for any function that touches transactions, locks, money arithmetic, or aggregations. Those four areas have a small number of recurring failure patterns. Once you know what to look for, catching them is quick.
