# Playto Pay — Payout Engine

A payout engine for Indian merchants collecting international payments. Merchants accumulate balance from customer payments and request INR withdrawals to their bank accounts. The engine moves each payout through `pending → processing → completed | failed` with correct concurrency control, idempotency, and state-machine enforcement.

The core logic lives in `backend/ledger/` — primarily `services.py`, `tasks.py`, and `models.py`. The HTTP layer in `views.py` is intentionally thin: each view validates input and delegates to `services.py`. See `EXPLAINER.md` for the design reasoning behind each piece.

---

## Stack

| Layer     | Technology                        |
|-----------|-----------------------------------|
| Backend   | Django 5 + Django REST Framework  |
| Database  | PostgreSQL (BigInteger paise, no floats) |
| Worker    | Celery 5 + Redis (worker + beat scheduler) |
| Frontend  | React 18 + Vite + Tailwind CSS    |
| Dev env   | Docker Compose                    |

---

## Repository layout

```
backend/
  playto/                   Django project (settings, urls, celery)
  ledger/
    models.py               Merchant, BankAccount, LedgerEntry, Payout, IdempotencyKey
    services.py             Locking, balance computation, idempotency, state transitions
    tasks.py                Celery worker + stuck-payout reaper
    views.py                DRF Class-Based Views
    urls.py                 URL routing
    exceptions.py           Typed business errors mapped to clean 4xx responses
    serializers.py          DRF serializers (input validation only)
    management/commands/seed.py   Demo data
    tests/
      test_concurrency.py   Two threads racing for an overdraft
      test_idempotency.py   Same key replayed; same key with different body
      test_state_machine.py All legal and illegal state transitions
    migrations/
  requirements.txt
frontend/
  src/                      React single-page dashboard
  vite.config.js
  tailwind.config.js
docker-compose.yml          postgres + redis + web + worker + beat (local dev)
EXPLAINER.md                Deep-dive answers covering ledger design, locking, idempotency, and more
```

---

## Quick start — with Docker (recommended)

```bash
# 1. Start the backend services (postgres, redis, django, celery worker + beat)
docker compose up --build

# 2. In a separate terminal, start the frontend dev server
cd frontend
npm install
npm run dev
```

- Backend API: http://localhost:8000
- Frontend dashboard: http://localhost:5173 (proxies `/api` to the backend)
- Django admin: http://localhost:8000/admin

The `web` service automatically runs `migrate` and `seed` on first boot, so demo merchants and credit history are ready immediately.

---

## Quick start — without Docker

You will need PostgreSQL and Redis running locally before starting.

```bash
# --- Backend ---
cd backend
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Create a .env file with your local DB and Redis URLs:
echo "DATABASE_URL=postgres://playto:playto@localhost:5432/playto" > .env
echo "REDIS_URL=redis://localhost:6379/0" >> .env
echo "DJANGO_SECRET_KEY=dev-only-change-in-prod" >> .env
echo "DEBUG=1" >> .env

python manage.py migrate
python manage.py seed            # populates demo merchants and credit history

# Terminal 1 — Django web server
python manage.py runserver

# Terminal 2 — Celery worker (processes payouts)
celery -A playto worker -l info

# Terminal 3 — Celery beat (runs the stuck-payout reaper every 10s)
celery -A playto beat -l info
```

```bash
# --- Frontend ---
cd frontend
npm install
npm run dev
```

---

## Running the tests

```bash
cd backend
python manage.py test ledger
```

Three test suites run:

- **test_concurrency.py** — Two threads each request a 60 INR payout against a 100 INR balance. Exactly one succeeds; the other raises `InsufficientBalance`. Uses `TransactionTestCase` (real commits, real locks) rather than Django's default test-transaction rollback, so the `SELECT FOR UPDATE` serialization is actually exercised.

- **test_idempotency.py** — Verifies that replaying the same idempotency key returns the identical cached response and creates no duplicate payout. Also verifies that reusing a key with a different request body is rejected with `422 idempotency_conflict`.

- **test_state_machine.py** — Walks every legal and every illegal state transition individually, confirming `InvalidTransition` is raised for anything backwards, sideways, or out of a terminal state.

---

## API reference

All endpoints are under `/api/v1/`. Merchant-scoped endpoints require an `X-Merchant-Id` header (UUID). In production this would be an API-key middleware lookup; here it is kept simple so the API can be exercised with any HTTP client.

| Endpoint                | Method | Description                                           |
|-------------------------|--------|-------------------------------------------------------|
| `/merchants`            | GET    | List all merchants (used by the demo dashboard)       |
| `/balance`              | GET    | `{settled_paise, held_paise, available_paise}`        |
| `/ledger`               | GET    | Last 50 credit/debit entries, newest first            |
| `/bank-accounts`        | GET    | Bank accounts registered to the merchant              |
| `/payouts`              | GET    | Last 50 payouts, newest first                         |
| `/payouts`              | POST   | Create a payout (requires `Idempotency-Key` header)   |
| `/payouts/<uuid>`       | GET    | Single payout detail                                  |

### Creating a payout

```http
POST /api/v1/payouts
Content-Type: application/json
X-Merchant-Id: 11111111-1111-1111-1111-111111111111
Idempotency-Key: 6a37c9b1-0000-0000-0000-000000000001

{
  "amount_paise": 500000,
  "bank_account_id": "aaaaaaaa-1111-1111-1111-111111111111"
}
```

**Response (201 Created):**
```json
{
  "id": "...",
  "amount_paise": 500000,
  "status": "pending",
  "attempts": 0,
  "bank_account": { ... },
  "created_at": "..."
}
```

Replaying the same `Idempotency-Key` within 24 hours returns the identical response and creates no second payout. Replaying with the same key but a different body returns `422 idempotency_conflict`.

---

## Seed data

The `seed` command creates three demo merchants with pre-seeded credit history:

| Merchant            | Available balance (approx.) |
|---------------------|-----------------------------|
| Studio Bombay       | ₹1,37,500                   |
| Pixel Forge Agency  | ₹1,65,000                   |
| Mira Freelance      | ₹38,500                     |

Re-running `python manage.py seed` resets all demo data to this clean state.

> **Important for deployments:** `seed` wipes all existing payouts and ledger entries. Run it once after initial deploy, then remove it from your startup command. If your platform restarts the web process automatically (e.g. Render free tier after inactivity), do not include `seed` in the start command or all payout history will be lost on each restart.

---

## Money rules

These are enforced throughout the codebase and are non-negotiable:

- All amounts are stored as `BigIntegerField` paise (integers). No `FloatField`, no `DecimalField`, no Python float arithmetic on money.
- The ledger is append-only. `LedgerEntry` rows are never updated or deleted. Replaying them in `created_at` order always recovers exact history.
- Balance is recomputed from SQL aggregations (`SUM ... FILTER`) on every read. There is no stored balance column that could drift.
- Holds are implicit: a `Payout` in `pending` or `processing` state counts as a hold against the merchant's available balance. Releasing a hold is a single-row UPDATE that flips the status to `failed`.

See `EXPLAINER.md` for the full reasoning behind each of these choices.

---

## What is not included

- Customer payment ingestion — credits are seeded directly; the inbound USD payment flow is out of scope.
- Real bank rails — the worker simulates settlement (70% success, 20% failure, 10% hang).
- Authentication — `X-Merchant-Id` stands in for what would be an API-key middleware in production.
- Webhooks — an optional bonus; omitted to keep scope focused.
