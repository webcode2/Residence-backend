# Resident Pass Backend (Residence SaaS)

A High-performance Multi-tenant IoT Access Control SaaS built with FastAPI, SQLAlchemy 2.0, and PostgreSQL.

## 🚀 Key Features

*   **Multi-Tenancy**: Built-in data isolation using `X-App-Id`.
*   **IoT Ready**: High-speed RFID and Visitor Token verification endpoints (O(1) lookup).
*   **Property Hierarchy**: Residents automatically inherit property details (House Number/Street) from Landlords.
*   **Bulk Landlord Import**: Automated account creation and email notifications via SendPulse.
*   **Secure Registration**: 9-character alphanumeric registration codes with revocation support.
*   **RBAC**: Role-based access control (Admin, Landlord, Resident, Security).

## 🛠 Tech Stack

*   **FastAPI**: Web framework.
*   **SQLAlchemy 2.0**: Database toolkit (Async).
*   **Alembic**: Database migrations.
*   **PostgreSQL**: Primary database.
*   **SendPulse**: SMTP/Transactional email service.
*   **Pytest**: Comprehensive test suite.

## 🏁 Getting Started

### 1. Environment Setup
```bash
cp .env.example .env
# Fill in your DATABASE_URL and SENDPULSE credentials
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

Run the full test suite (including bulk import tests):
```bash
./run_tests.sh
```

## 📚 API Overview

*   **`/api/v1/auth`**: JSON-based login.
*   **`/api/v1/users`**: User management & Resident registration.
*   **`/api/v1/landlords/import`**: Bulk import landlords.
*   **`/api/v1/tokens`**: Generate and revoke registration/visitor codes.
*   **`/api/v1/verify`**: High-speed verification for IoT devices.

## 🛣 Next Steps (Roadmap)

- [ ] **Frontend Integration**: Transition to using the `X-App-Id` header across all frontend modules.
- [ ] **Dockerization**: Create `Dockerfile` and `docker-compose.yml` for production deployments.
- [ ] **Redis Caching**: Implement Redis for JWT blacklisting and token verification speed-ups.
- [ ] **Payment Integration**: Connect Paystack/Flutterwave for automated service charge billing.
- [ ] **Swagger UI Customization**: Brand the `/docs` page for estate admins.
