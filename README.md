# Playto Pay — Payout Engine

A minimal but production-shaped payout engine for the Playto founding-engineer
challenge. Indian merchants accumulate USD balance, request INR payouts, and
the engine moves the money through `pending → processing → completed | failed`
with proper concurrency, idempotency, and state-machine guards.

The interesting code lives in `backend/ledger/` — primarily `services.py`,
`tasks.py`, and `models.py`. The HTTP layer is thin: `views.py` is built
on DRF `APIView` Class-Based Views, each one delegating straight into
`services.py`. See `EXPLAINER.md` for why each piece is shaped the way it
is.

## Stack

- **Backend:** Django 5 + DRF + Postgres
- **Worker:** Celery 5 + Redis (worker + beat)
- **Frontend:** React 18 + Vite + Tailwind CSS
- **Deploy:** Render (blueprint in `render.yaml`); local dev via `docker compose`

## Repo layout

```
backend/
  playto/                 # Django project (settings, urls, celery wiring)
  ledger/
    models.py             # Merchant, BankAccount, LedgerEntry, Payout, IdempotencyKey
    services.py           # Locking, balance, idempotency, state transitions
    tasks.py              # Celery worker + stuck-payout reaper
    views.py              # DRF Class-Based Views (APIView)
    urls.py               # Routes -> CBVs via .as_view()
    exceptions.py         # Typed errors mapped to clean 4xx responses
    management/commands/seed.py
    tests/
      test_concurrency.py
      test_idempotency.py
      test_state_machine.py
    migrations/0001_initial.py
  Dockerfile
  requirements.txt
frontend/
  src/                    # React dashboard (single-page)
  vite.config.js, tailwind.config.js
docker-compose.yml        # postgres + redis + web + worker + beat
render.yaml               # Render blueprint
EXPLAINER.md              # The deep-dive answers the rubric asks for
```

## Quick start (local, with Docker)

```bash
docker compose up --build
# In another terminal:
cd frontend && npm install && npm run dev
```

- Backend: <http://localhost:8000>
- Frontend dev server: <http://localhost:5173> (proxies /api to the backend)
- Django admin: <http://localhost:8000/admin> (create a superuser if you need it)

The `web` service runs `migrate` and `seed` on boot, so the demo merchants
are populated from first launch.

## Quick start (local, without Docker)

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # adjust DATABASE_URL / REDIS_URL if needed
python manage.py migrate
python manage.py seed
python manage.py runserver

# In a second terminal:
celery -A playto worker -l info
# In a third terminal (for the retry reaper):
celery -A playto beat -l info

# Frontend
cd frontend
npm install
npm run dev
```

You'll need Postgres and Redis running locally. The simplest path is the
docker-compose flow above.

## Running tests

```bash
cd backend
python manage.py test ledger
```

Three suites:

- `test_concurrency.py` — two threads racing for an overdraft. Uses
  `TransactionTestCase` so locking is real.
- `test_idempotency.py` — same key replayed; same key with a different body.
- `test_state_machine.py` — every legal and illegal transition.

## API quick reference

All endpoints are under `/api/v1/` and require an `X-Merchant-Id` header
(merchant UUID). Replace this with API-key auth in production.

| Endpoint                       | Method | Purpose                                |
|--------------------------------|--------|----------------------------------------|
| `/merchants`                   | GET    | List merchants (demo helper)           |
| `/balance`                     | GET    | `{settled, held, available}` in paise  |
| `/ledger`                      | GET    | Recent credits/debits (last 50)        |
| `/bank-accounts`               | GET    | Merchant's bank accounts               |
| `/payouts`                     | GET    | Recent payouts (last 50)               |
| `/payouts`                     | POST   | Create a payout (requires Idempotency-Key) |
| `/payouts/<uuid>`              | GET    | One payout                             |

### Create payout

```http
POST /api/v1/payouts
X-Merchant-Id: 11111111-1111-1111-1111-111111111111
Idempotency-Key: 6a37c9b1-…
Content-Type: application/json

{ "amount_paise": 500000, "bank_account_id": "aaaaaaaa-1111-1111-1111-111111111111" }
```

Same key replayed within 24h → exact same response. Same key with a different
body → 422 `idempotency_conflict`.

## Deploy on Render

1. Push the repo to GitHub.
2. In Render, "New" → "Blueprint" → point at the repo.
3. Render reads `render.yaml` and provisions Postgres, Redis, web, worker,
   beat, and a static-site frontend.
4. After first deploy, set `VITE_API_BASE` on `playto-frontend` to the
   backend URL (e.g. `https://playto-web.onrender.com`) and redeploy the
   frontend.

The backend's `buildCommand` runs `migrate` and `seed`, so production starts
with the same demo merchants you see locally.

## Money rules (non-negotiables)

- All amounts are integers in paise. No floats anywhere — not in models, not
  in serializers, not in calculations.
- The ledger is append-only. We never UPDATE or DELETE rows in
  `LedgerEntry`. Holds are tracked via the `Payout` table itself.
- Balance is recomputed from SQL aggregations on every read; we never store
  a denormalized balance column.

See `EXPLAINER.md` for the full reasoning.

## What's deliberately not here

- Customer payment ingestion (CREDITs are seeded, not produced by a flow).
- Real bank rails. The worker rolls a die per the spec (70/20/10).
- Auth. `X-Merchant-Id` stands in for what would otherwise be an API-key
  middleware lookup.
- Webhooks. Mentioned as an optional bonus; skipped to keep the surface
  area small.
