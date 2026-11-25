# Resident Pass Backend (Residence SaaS)

A High-performance Multi-tenant IoT Access Control SaaS built with FastAPI, SQLAlchemy 2.0, and PostgreSQL.

## 🚀 Key Features

*   **Multi-Tenancy**: Built-in data isolation using `X-App-Id`.
*   **Subscription Tiers & Quota Management**: 4 distinct subscription tiers ($15, $30, $70, $100/mo) with verification quota tracking, RFID feature flags, and automatic billing cycles.
*   **IoT Ready Access Control**: High-speed RFID and Visitor Token verification endpoints (O(1) lookup).
*   **Real-Time Alert System**: Instant security alerts and visitor arrival notifications to residents and admins via Resend.
*   **Tiered Audit Log Retention**: Audit trail and entry logging bounded by tier retention windows (30, 90, 180, and 365 days).
*   **Property Hierarchy**: Residents automatically inherit property details (House Number/Street) from Landlords.
*   **Bulk Landlord Import**: Automated account creation and email notifications via Resend.
*   **Secure Registration**: 9-character alphanumeric registration codes with revocation support.
*   **RBAC**: Role-based access control (Admin/Caretaker, Landlord, Resident, Security).

---

## 💎 SaaS Subscription Plans & Tiers

| Plan Tier | Price / Month | Monthly Verifications | RFID Gate Access | Log Retention History | Real-Time Alerts | Ideal For |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Starter** | **$15** | 20,000 / month | ❌ Disabled (Passcodes only) | **30 days** | ❌ Basic logs only | Small gated communities & passcodes |
| **Standard** | **$30** | 50,000 / month | ✅ Full RFID Support | **90 days** (3 months) | ✅ Instant check-in alerts | Mid-sized estates with boom gates |
| **Premium** | **$70** | 300,000 / month | ✅ Full RFID Support | **180 days** (6 months) | ✅ Priority real-time alerts | High-traffic estates & communities |
| **Enterprise** | **$100** | **Unlimited** | ✅ Full RFID Support | **365 days** (1 year) | ✅ Priority multi-channel alerts | Large commercial & residential estates |

---

## 🛠 Tech Stack

*   **FastAPI**: Web framework.
*   **SQLAlchemy 2.0**: Database toolkit (Async).
*   **Alembic**: Database migrations.
*   **PostgreSQL**: Primary database.
*   **BACHS Payment Gateway**: Hosted checkout sessions, webhook fulfillment & subscription billing.
*   **Resend**: High-deliverability transactional email service & real-time alerts.
*   **Pytest**: Comprehensive test suite.

## 💳 Estate Admin Checkout Flow (BACHS)

Estate admins (`caretaker` role) can upgrade or renew plans via hosted BACHS checkout:

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/v1/subscriptions/checkout` | Create hosted checkout session (returns `checkout_url`) |
| `GET` | `/api/v1/subscriptions/checkout/{session_id}/verify` | Verify return redirect and fulfill subscription |
| `POST` | `/api/v1/subscriptions/webhook/bachs` | Inbound webhook with HMAC-SHA256 signature verification |

*(Also available under `/api/v1/payments/*`)*

## ⏱ Automated Platform Maintenance & Background Workers

The platform includes background routines for time-dependent lifecycle management:
1. **Tier Retention Purge**: Deletes `access_logs` older than each estate's plan retention window (30/90/180/365 days).
2. **Quota & Billing Reset**: Resets monthly verification counts (`current_month_verifications = 0`) on 30-day billing cycle intervals and marks expired subscriptions as `expired`.
3. **Token Cleanup**: Transitions unredeemed visitor tokens past 24 hours to `expired` and clears stale exit codes.

### Execution Options:
* **Embedded API workers**: For simple/dev single-process runs, the API lifespan can start background job workers and the maintenance scheduler in-process. Controlled by `ENABLE_EMBEDDED_WORKERS` and `ENABLE_MAINTENANCE_SCHEDULER`.
* **Dedicated worker process (recommended for production)**:
  ```bash
  python worker.py          # Runs a single maintenance pass and exits (ideal for K8s CronJobs / Linux crontab)
  python worker.py --daemon # Runs continuously: Redis queue drains + maintenance + landlord onboarding + SOS outbox + history jobs
  ```
  In production compose, set `ENABLE_EMBEDDED_WORKERS=false` and `ENABLE_MAINTENANCE_SCHEDULER=false` on API instances and run the `worker` service.
* **On-Demand Admin Endpoint**:
  ```http
  POST /api/v1/admin/maintenance/run
  Authorization: Bearer <saas_admin_token>
  ```


## 🏁 Getting Started

### 1. Environment Setup
```bash
cp .env.example .env
# Fill in DATABASE_URL, REDIS_URL (Upstash), RESEND credentials, and BACHS API keys
```

> **MVP architecture (`mvp` branch):** PostgreSQL is remote via Neon (`DB_URL`) and Redis is remote via Upstash (`REDIS_URL`). There are no local Postgres, PgBouncer, or Redis containers. Set both URLs in `.env` before starting the stack:
> ```bash
> docker compose -f docker-compose.prod.yml up -d --build
> ```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Database Initialization
```bash
# Initialize schema
python init_db.py

# Run migrations if any
./migrate.sh "initial"
```

### 4. Run the Server
```bash
uvicorn app.main:app --reload
```

## 🧪 Testing

Run the full test suite:
```bash
./run_tests.sh
```

## 📚 API Overview

*   **`/api/v1/auth`**: JSON-based login & JWT generation.
*   **`/api/v1/users`**: User management & Resident registration.
*   **`/api/v1/subscriptions`**: List plans/tiers, view usage metrics, and upgrade/change tier.
*   **`/api/v1/access-logs`**: Query verification history (strictly bounded by tier log retention) and log cleanup.
*   **`/api/v1/landlords/import`**: Bulk import landlords.
*   **`/api/v1/tokens`**: Generate entry codes, generate book-out passes (`/visitor/{token_id}/book-out`), and revoke tokens.
*   **`/api/v1/verify`**: High-speed IoT verification for entry (`/token/{code}` and `/rfid/{tag}`) and exit (`/book-out/{code}`).
*   **`/api/v1/admin`**: Dedicated SaaS Super-Admin portal (Platform overview KPIs, Estates management & suspension, Cross-tenant Subscription overrides, Global User search/revocation, Platform security audit feed).

## 🛣 Next Steps (Roadmap)

- [x] **Dockerization**: `docker-compose.yml` with PostgreSQL and Redis 7.
- [x] **Redis Caching**: Sub-millisecond visitor tokens, subscription tier cache, JWT blacklisting, and user session cache.
- [ ] **Frontend Integration**: Transition to using the `X-App-Id` header across all frontend modules.
- [ ] **Swagger UI Customization**: Brand the `/docs` page for estate admins.

## Demo seeding and owner administration

`python seed.py` adds four clearly named demo estates and their role accounts, gates, conversations, visitor passes, and access history. It preserves existing data and does not reset existing passwords. A known legacy seed-owner hash using the former default password is repaired using the dedicated owner password hash. Re-running the script adds no duplicate records and does not reset subscriptions or conversation state.

New passwords are generated securely and written to `.seed-credentials.json` with mode `0600`; passwords are not printed. Optional `SEED_OWNER_EMAIL` and `SEED_OWNER_PASSWORD` configure a new owner account. The password must contain at least 8 characters. Keep generated credential files private and out of Git.

Apply `alembic upgrade head` before seeding to install the additive `admin_audit_logs` migration. Owner subscription, estate-status, and user-access changes write an audit event in the same database transaction as the change.

The verified isolated demo dataset for this task is in `residence_owner_demo_20261022`, with credentials in `.seed-demo-credentials.json`. The production-configured `residence_db` was left unchanged. The isolated preview backend uses Redis database 14 and disables maintenance scheduling; test runs use the separate `residence_owner_demo_20261022_test` database and Redis database 13.

Always select the intended database explicitly before running the seed command. The checked-in `.env` may describe the production-configured Docker database; it is not automatically redirected to the isolated demo database. The task's preview processes use explicit runtime configuration, not changes to `.env`.

To run the isolated preview again from `backend/`, use `PYTHONPATH=. /tmp/residence-check-venv/bin/python scripts/run_demo_preview.py` (or your installed backend Python environment). This binds only `127.0.0.1:8002`. From `Admin/`, run `RESIDENCE_BUILD_DIR=.next-owner-check NEXT_PUBLIC_BACKEND_API_URL=http://127.0.0.1:8002/api/v1/ SESSION_SECRET=<a-local-random-secret> npm run dev -- --hostname 127.0.0.1 --port 3001`. Visit `/owner/login` for the owner or `/login` for an estate caretaker. Keep the preview session secret stable between runs if you want existing preview cookies to remain valid.


### Editable plan catalogue and upfront billing

The SaaS owner edits plan names, descriptions, monthly USD prices, verification limits,
features, and separate 6-month / 12-month discount percentages at `/owner/plans`.
Discounts default to 0; valid values are 0–99.99, with at most two decimal places.
Edits are audited and version checked. Existing estate subscriptions are not rewritten.
New estate creation, owner plan assignment, and renewals use the stored catalogue.

Caretakers select 1, 6, or 12 months at `/dashboard/subscription`. Checkout stores its
price, term, and entitlements before redirecting to the existing BACHS integration.
Fulfillment extends the subscription by calendar months and consumes each checkout
once, whether confirmation arrives through verification or a signed webhook. A later
catalogue edit cannot change a saved purchase. Unpaid caretaker tier changes are blocked.
Live BACHS credentials and a webhook signing secret are required for real payments;
simulated sessions cannot activate subscriptions in production mode.

Apply migration `cd315e6f7082` before starting the updated API. Local preview validation
uses only `residence_owner_demo_20261022`; production migration/deployment is separate.


### Bachs sandbox verification

The checkout adapter reads Bachs `checkout_id`, sends a unique `reference` and
`Idempotency-Key`, and verifies `payment_status` / successful charge status rather
than treating `status=completed` alone as proof of payment. Both webhook routes
accept timestamped `X-Bachs-Signature-V2`, legacy timestamped signatures, and raw
HMAC signatures. Successful collection events are reconciled against authenticated
checkout retrieval, so converted buyer currencies do not corrupt USD plan quotes.
No periodic browser polling is used.

Bachs rejects loopback return URLs. Sandbox checkout on localhost therefore opens
in another tab and the caretaker uses **Confirm payment** on return. On a public
app origin, normal success/cancel redirects are used; success triggers one server
verification, and cancellation does not grant access.

For local signed-event tests install the official CLI in a temporary directory,
set `BACHS_API_KEY` to your sandbox key, and run:

```sh
bachs listen --forward-to localhost:8002/api/v1/payments/webhook/bachs --events collection.succeeded,collection.failed,checkout.completed
```

Supply the listener's signing secret as `BACHS_WEBHOOK_SECRET` to the local API
process. Do not commit it or use the temporary listener secret for production.
For deployment register your public HTTPS `/api/v1/payments/webhook/bachs` URL in
Bachs and set the signing secret issued for that endpoint. Use matching sandbox
or live API keys and base URLs. No public key is required by this hosted-checkout
backend integration.

### Card-backed estate activation

New self-service estates register with `pending_activation` access. Their caretaker selects a monthly plan, accepts the displayed recurring charge, and completes a Bachs hosted USD-card checkout. `ESTATE_TRIAL_DAYS` defaults to 14. Bachs stores the card and schedules the first charge after the trial; this application never receives card numbers or CVVs. Existing estates and owner-created subscriptions are unchanged.

The additive migration `de426f708193` records the consent, immutable plan price and provider references. Configure Bachs webhook delivery to `/api/v1/payments/webhook/bachs` (or the existing subscription webhook), with `BACHS_WEBHOOK_SECRET`, and enable `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, and `invoice.payment_failed`. Webhook signatures are checked by the existing handler; subscription state is retrieved from the authenticated provider API before access is changed. Replays do not add extra time. Failed billing suspends access; Bachs performs payment recovery. The dashboard can refresh provider state and cancel renewal at the end of the trial/current period. Cancellation before trial expiry prevents the first scheduled charge.

New activation uses monthly recurring subscriptions. Existing one-time 1/6/12-month checkout remains available for estates without an automatic subscription. Plan edits affect new activation quotes; existing card subscriptions keep the price accepted at setup. Sandbox keys create sandbox subscriptions only; use live Bachs credentials and a public webhook endpoint for actual charges.
