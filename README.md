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
* **Built-in Async Scheduler**: Runs automatically inside the FastAPI process on a configured interval (default: 1 hour). Configured via `ENABLE_MAINTENANCE_SCHEDULER` and `MAINTENANCE_INTERVAL_SECONDS`.
* **Standalone CLI Worker (CronJob / Orchestrator)**:
  ```bash
  python worker.py          # Runs a single maintenance pass and exits (ideal for K8s CronJobs / Linux crontab)
  python worker.py --daemon # Runs continuously as an independent background daemon
  ```
* **On-Demand Admin Endpoint**:
  ```http
  POST /api/v1/admin/maintenance/run
  Authorization: Bearer <saas_admin_token>
  ```


## 🏁 Getting Started

### 1. Environment Setup
```bash
cp .env.example .env
# Fill in DATABASE_URL, RESEND credentials, and BACHS API keys
```

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
