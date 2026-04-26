# EXPLAINER

These are the answers to the questions the rubric asks. Every code snippet
is pasted from the actual repo and is a copy of what runs in production.

---

## 1. The Ledger

**My balance calculation, in code:**

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
    debits = settled["debits"] or 0

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

That ORM call compiles to roughly:

```sql
SELECT
  COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'CREDIT'), 0) AS credits,
  COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'DEBIT'),  0) AS debits
FROM ledger_ledgerentry
WHERE merchant_id = %s;

SELECT COALESCE(SUM(amount_paise), 0) AS held
FROM ledger_payout
WHERE merchant_id = %s
  AND status IN ('pending','processing');
```

`available = (credits − debits) − held`.

**Why this model:**

I considered the four-event ledger (CREDIT, DEBIT, HOLD, RELEASE) and the
double-entry style with two rows per movement. I picked the simplest one
that meets the spec — single `LedgerEntry` table with `kind ∈ {CREDIT, DEBIT}`,
amounts always positive, sign comes from the kind. Holds are *implicit*: a
`Payout` in `pending`/`processing` is the hold. The table doesn't need a
separate row for it.

Three reasons:

1. **The spec literally says "credits and debits."** Sticking to that
   vocabulary keeps the model legible to anyone who reads it cold.
2. **The ledger stays append-only.** We never UPDATE or DELETE rows in
   `LedgerEntry`. Replaying the rows in `created_at` order recovers exact
   history — no compensating entries needed when payouts fail (because
   nothing was written in the first place).
3. **One source of truth for "where's the hold."** It's the `Payout.status`
   column. Releasing a hold is a single UPDATE on one row, atomic by
   definition. With a separate HOLD/RELEASE table you have two writes that
   need to stay in lockstep, and any drift is silent corruption.

The trade-off: balance reads do two aggregations instead of one. Both run
under indexes (`merchant_id, created_at` on ledger; `merchant_id, status`
implicit via the partial-where pattern). At our scale this is fine. If we
ever hit it, materializing balance into a `merchant_balance` table updated
by a transactional trigger or service-layer write is a known refactor.

**Why integer paise, not Decimal:**

`BigIntegerField` paise gives us 2^63 ≈ ~9.2 quintillion paise of headroom,
which is enough universe. Floats are a non-starter (0.1 + 0.2 ≠ 0.3).
`DecimalField` would also work, but it's less defensible: every dev has to
remember which arithmetic ops preserve precision and which silently coerce.
With integers there's nothing to forget.

---

## 2. The Lock

**The exact code that prevents two concurrent payouts from overdrawing:**

```python
# backend/ledger/services.py

def lock_merchant(merchant_id) -> Merchant:
    """Acquire a row-level exclusive lock on the merchant row.

    MUST be called inside a transaction. Postgres will block any other
    transaction that calls this on the same merchant until we commit.
    """
    return Merchant.objects.select_for_update().get(pk=merchant_id)


@transaction.atomic
def create_payout(*, merchant_id, bank_account_id, amount_paise: int) -> Payout:
    if amount_paise <= 0:
        raise LedgerError("amount must be positive")

    merchant = lock_merchant(merchant_id)              # <-- the lock
    balance = compute_balance(merchant.id)

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

**The database primitive it relies on:**

`select_for_update()` in Django emits `SELECT … FOR UPDATE` against
Postgres. That acquires a **row-level exclusive lock** on the merchant
row. Any other transaction that does `SELECT … FOR UPDATE` on the same row
blocks until the first transaction commits or rolls back. Read-only
queries without `FOR UPDATE` aren't blocked — they see the pre-transaction
snapshot under Read Committed.

This is exactly the primitive needed to defeat the classic check-then-act
race:

> Without the lock:
>   T1: SELECT balance → 100
>   T2: SELECT balance → 100      (sees same snapshot)
>   T1: INSERT payout 60          → balance now 40
>   T2: INSERT payout 60          → balance now -20 ☠
>
> With the lock:
>   T1: SELECT … FOR UPDATE on merchant     (row locked)
>   T1: SELECT balance → 100
>   T2: SELECT … FOR UPDATE on merchant     **blocks**
>   T1: INSERT payout 60; COMMIT             (lock released)
>   T2: unblocked; SELECT balance → 40
>   T2: 40 < 60, raise InsufficientBalance ✓

**Why I lock the merchant row, not the payouts table or a balance row:**

The invariant is per-merchant: two payouts from *different* merchants are
independent and shouldn't serialize. The merchant row is the natural
per-merchant mutex. There's no balance row to lock (we don't store one),
and locking "all payouts for this merchant" is awkward — you'd need
`SELECT … FOR UPDATE` over a query result set, which Postgres lets you do
but adds nothing over locking the merchant.

The test that proves this works is `test_two_simultaneous_payouts_for_more_than_balance`
in `backend/ledger/tests/test_concurrency.py`. It uses
`TransactionTestCase` (real commits between threads, not Django's
test-transaction-rollback magic) and `threading.Barrier` to fire two
threads at the same instant. One thread's payout succeeds, the other's
raises `InsufficientBalance`. Always. Run it 50× — same result.

**One thing to be careful about:** I deliberately do *not* hold any DB
locks during the slow bank-call simulation in the worker. The worker takes
the merchant lock briefly, transitions `pending → processing`, releases.
Sleeps. Re-acquires the payout's row lock to transition to terminal.
Holding a lock through a 30-second external call would jam the entire API.

---

## 3. The Idempotency

**How the system knows it has seen a key before:**

`IdempotencyKey` table with a `UNIQUE(merchant_id, key)` constraint. The
unique constraint *is* the serialization point — Postgres guarantees at
most one row exists per (merchant, key) regardless of how many concurrent
INSERTs race for it.

The flow (from `services.idempotent`):

```python
# 1. Try to claim the key by INSERTing. The UNIQUE constraint makes this
#    atomic across connections — exactly one INSERT wins.
try:
    with transaction.atomic():
        ik = IdempotencyKey.objects.create(
            merchant_id=merchant_id,
            key=key,
            request_fingerprint=request_hash,
        )
    reserved_now = True
except IntegrityError:
    reserved_now = False

# 2a. We won the race: run the handler, then write the response back.
if reserved_now:
    try:
        http_status, body, payout = handler()
    except Exception:
        IdempotencyKey.objects.filter(pk=ik.pk).delete()  # clear the reservation
        raise
    IdempotencyKey.objects.filter(pk=ik.pk).update(
        response_status=http_status,
        response_body=body,
        payout=payout,
        completed_at=timezone.now(),
    )
    return http_status, body

# 2b. We lost the race: read the existing row.
ik = IdempotencyKey.objects.get(merchant_id=merchant_id, key=key)
if ik.request_fingerprint != request_hash:
    raise IdempotencyConflict("idempotency key reused with a different payload")
if ik.is_complete:
    return ik.response_status, ik.response_body
raise IdempotencyInFlight("request with this idempotency key is still being processed")
```

The `request_fingerprint` is `sha256(canonical_json(payload))`. Sorted keys,
no whitespace. Same payload → same hash. Different payload with the same
key → fingerprint mismatch → 422 `idempotency_conflict`. That's a real bug
on the client; we refuse rather than guess.

**What happens if the first request is in flight when the second arrives:**

The reservation INSERT in step 1 commits *before* the handler runs. So the
moment request A has reserved the key, request B (arriving milliseconds
later) sees A's row in the database. B's INSERT raises `IntegrityError`.
B reads the row, sees `completed_at IS NULL`, and returns
`409 IdempotencyInFlight`.

I deliberately don't have B block on A. Here's the trade-off:

- **Block:** B waits, eventually returns the same 201. UX is "did the
  request work?" → yes. But B's HTTP connection is held open for as long
  as A takes, which can be seconds (bank rails). On a multi-instance
  deploy this also requires advisory locks because B might land on a
  different web process than A.
- **Refuse with 409 (what I picked):** B fails fast with a meaningful
  error code. Clients with proper retry logic (which is who uses
  idempotency keys in the first place) handle this — they retry after a
  short delay and either get the cached 201 or another 409. Simple,
  predictable, no hidden lock-wait latency.

**Keys are scoped per merchant** because the merchant column is part of
the unique constraint. Two different merchants can use the same key string
without collision — important since clients pick their own UUIDs.

**Keys expire after 24h** via the `purge_expired_idempotency_keys` Celery
beat task, which runs hourly:

```python
# backend/ledger/tasks.py
@shared_task
def purge_expired_idempotency_keys() -> int:
    cutoff = timezone.now() - settings.IDEMPOTENCY_KEY_TTL  # 24h
    deleted, _ = IdempotencyKey.objects.filter(created_at__lt=cutoff).delete()
    return deleted
```

After expiry, the same key can be reused for a new request — appropriate,
since after 24 hours nobody is retrying anymore.

The test `test_replay_returns_cached_response` proves the cache returns
*the same bytes* (it asserts `body_a == body_b`, not just same payout id).
The test `test_same_key_different_payload_is_rejected` proves the
fingerprint check works.

---

## 4. The State Machine

**Where `failed → completed` is blocked:**

`backend/ledger/models.py`:

```python
class Payout(TimestampedModel):
    class Status(models.TextChoices):
        PENDING    = "pending",    "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED  = "completed",  "Completed"
        FAILED     = "failed",     "Failed"

    LEGAL_TRANSITIONS = {
        Status.PENDING:    {Status.PROCESSING},
        Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
        Status.COMPLETED:  set(),  # terminal
        Status.FAILED:     set(),  # terminal
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

`failed → completed` is rejected because `LEGAL_TRANSITIONS[Status.FAILED]`
is the empty set. Any `new_status` not in the empty set fails the `not in
allowed` check, which is *every* status. Same for `completed → anything`,
backwards moves like `processing → pending`, sideways skips like
`pending → completed`.

The check is purely table-driven — no `if/elif` ladders that drift over
time. Adding a new state is two edits: the enum and one row in the dict.

**Atomic state transition + side-effect:**

The state machine guard alone isn't enough; the side effects of a
transition have to commit *with* it. Two examples from `services.py`:

```python
@transaction.atomic
def mark_completed(payout_id) -> None:
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    payout.transition_to(Payout.Status.COMPLETED)   # state guard
    payout.completed_at = timezone.now()
    payout.save(update_fields=["status", "completed_at", "updated_at"])
    LedgerEntry.objects.create(                     # the debit
        merchant=payout.merchant,
        kind=LedgerEntry.Kind.DEBIT,
        amount_paise=payout.amount_paise,
        description=f"Payout {payout.id}",
        payout=payout,
    )
```

Two writes — the status flip and the DEBIT row — in one transaction. Both
commit or neither does. There is no window in which the payout reads
"completed" without a corresponding debit. That's the invariant the
rubric is asking for.

```python
@transaction.atomic
def mark_failed(payout_id, *, reason: str) -> None:
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    payout.transition_to(Payout.Status.FAILED)
    payout.failure_reason = reason[:255]
    payout.save(update_fields=["status", "failure_reason", "updated_at"])
```

Failed-payout-returns-funds is *atomic with the state transition* because
the "hold" is implicit. Funds are held by virtue of `status ∈
{pending, processing}`. The single UPDATE that flips `status → failed`
*is* the release. No second write to coordinate, no compensating ledger
entry. That's the whole reason I picked the implicit-hold model in §1.

The state-machine tests (`test_state_machine.py`) walk through every legal
and every illegal transition individually.

---

## 5. The AI Audit

I used Claude as my pair programmer throughout. The most useful early
draft it gave me was the `create_payout` function — and it had a subtly
wrong locking pattern that I want to call out specifically because it's
the exact bug the rubric warns about.

**What it gave me first (paraphrased — this is the recurring AI trap):**

```python
@transaction.atomic
def create_payout(merchant_id, amount_paise, ...):
    # Check current balance
    current_balance = compute_balance(merchant_id)
    if current_balance.available < amount_paise:
        raise InsufficientBalance(...)
    # Lock merchant and create payout
    merchant = Merchant.objects.select_for_update().get(pk=merchant_id)
    return Payout.objects.create(merchant=merchant, amount_paise=amount_paise, ...)
```

**What's wrong:** the balance check happens *before* the lock. Two
concurrent transactions both compute `available = 100`, both pass the
guard, then both serialize on the lock and create their payouts. The
lock is doing nothing because the decision was already made when it was
acquired. This is the "Python-level locking" failure mode — the lock
exists, but it's not in the path of the check that depends on it.

**What I caught:** the balance read needs to be *inside* the locked
window. Specifically, after the lock acquisition, so that the second
transaction sees the first's committed payout when it reads.

**What I replaced it with** (and what the repo runs):

```python
@transaction.atomic
def create_payout(*, merchant_id, bank_account_id, amount_paise: int) -> Payout:
    if amount_paise <= 0:
        raise LedgerError("amount must be positive")

    merchant = lock_merchant(merchant_id)            # lock FIRST
    balance = compute_balance(merchant.id)           # then read

    if balance.available < amount_paise:
        raise InsufficientBalance(...)

    return Payout.objects.create(...)
```

Lock first, read inside, decide, write, commit. The order matters and
it's exactly the kind of one-line rearrangement that an autoregressive
model gets backwards because both orderings "look reasonable."

**Two other subtler things I rejected from AI suggestions during this
work:**

1. **An f-string in the SQL `Sum(filter=Q(...))` clause.** The model
   constants (`LedgerEntry.Kind.CREDIT`) work because Django builds them
   into a parameterized query. An early suggestion built the filter via
   string interpolation. Accepted that and we'd be one bug away from a
   classic SQL injection on a money table. Replaced with the constants.

2. **Using `.update()` for the state transition** instead of fetching →
   `transition_to` → `save`. `Payout.objects.filter(id=…).update(status='failed')`
   is faster, but it bypasses `transition_to`, which means the legal-
   transitions table isn't consulted. The state machine becomes
   advisory. Refused; kept the fetch-and-save pattern even though it's
   one extra round trip — the guard is the whole point.

The pattern that worked for me: AI for shape and starting drafts,
careful re-reading any function that touches transactions, locks, money
arithmetic, or aggregations. The bug surface in those four areas
collapses to a small number of recurring patterns; once you've trained
your eye on them you catch the errors quickly.
