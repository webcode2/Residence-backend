---
name: residence-architecture
description: Decisions, rules and procedures for the Residence estate app (FastAPI backend, Next.js Admin on Cloudflare, Flutter mobile with separate resident and gate modules, Cloud Run API + scale-to-zero worker, Neon Postgres, Upstash Redis). Read before changing backend request handling, caching, queues, auth/passwords, deployment, regions, or the mobile app structure, so agents do not undo or redo agreed work.
---

# Residence architecture: decisions and rules

These decisions were made with the project owner on 25 Nov 2026. Do not reverse any of
them without asking the owner. When you change something covered here, update this file
in the same change.

Identical copies live in `backend/`, `Admin/` and `mobile/` at
`.claude/skills/residence-architecture/SKILL.md` (and in the top-level `residence/`
folder). When you edit one, update all of them in the same change.

## 1. Repositories and branches

| Folder | GitHub repo | Working branch | Notes |
|---|---|---|---|
| `backend/` | `webcode2/Residence-backend` | `mvp` | FastAPI + SQLAlchemy async + Alembic |
| `Admin/` | `webcode2/Residence-pub_ad` | `ohio-api-host-rename` | Next.js on Cloudflare Workers (OpenNext) |
| `mobile/` | `webcode2/residence-pass-flutter-app` | `host-rename-release-ids` | Flutter, package name `estate` |

The Admin and mobile branches are **not merged into `main` yet**; the owner merges.
The top-level `residence/` folder is not a git repo.

## 2. Infrastructure (Google Cloud project `residence-elastrocloud`)

| Piece | Where | Name / URL |
|---|---|---|
| API | Cloud Run `us-east5` (Columbus) | `residence-api` → `https://residence-api-136222598323.us-east5.run.app` |
| Worker | Cloud Run `us-east5`, private | `residence-worker` → `https://residence-worker-136222598323.us-east5.run.app` |
| Wake-up queue | Cloud Tasks `us-east4` (not offered in us-east5) | `residence-worker` |
| Postgres | Neon, AWS `us-east-2` | secret `DB_URL` |
| Redis | Upstash **Global**, primary Ohio | secrets `REDIS_URL_US`, `UPSTASH_REDIS_REST_TOKEN_US` |
| Admin + web entry | Cloudflare Worker `residence-admin` | `https://residence.elastrocloud.com` (rewrites `/api/v1/*` to the API) |

- **Everything scales to zero; CPU only during requests** (`--cpu-throttling`,
  min-instances 0). The owner rejected always-on CPU and min-instances. Do not change.
- The Africa (`africa-south1`) deployment was deleted on purpose. Do not recreate it.
- Service accounts: `residence-api@` runs API and worker (Secret Manager accessor,
  Cloud Tasks enqueuer); `residence-tasks@` signs the OIDC token Cloud Tasks uses to call
  the private worker (has `run.invoker` on the worker only).
- Secrets live in **Secret Manager** only. Never paste secret values in chat, logs, or
  commits. To add one from `.env`, write it to a temp file, `gcloud secrets versions add
  --data-file`, delete the file. `.env`, `.env.local`, Firebase admin key and
  `.seed*.json` are git-ignored and excluded by `.gcloudignore`.
- **Runtime settings live on the Cloud Run services, not in `.env`.** `.env` is never in
  the image, and `gcloud run deploy --image` keeps each service's existing env vars. To
  change a setting, run `gcloud run services update <svc> --update-env-vars ...` on
  **both** `residence-api` and `residence-worker`.
- Both services must route 100% of traffic to the **latest** revision. A pinned revision
  silently ignores new deploys and settings (this hid the email-sender fix on 27 Nov
  2026). Check with `gcloud run services describe <svc> --format='yaml(status.traffic)'`;
  fix with `gcloud run services update-traffic <svc> --to-latest`.
- Email: Resend, sender `Support <noreply@residence.elastrocloud.com>` (domain
  `residence.elastrocloud.com` verified in Resend). Never use `onboarding@resend.dev`:
  Resend rejects it (403) for every recipient except the Resend account owner. The
  `RESEND_API_KEY` secret is send-only.

### Measured latency (from Cloud Run us-east5)
Postgres round trip ~22 ms; typical 1-query request ~66 ms; Redis GET/SET ~24 ms. GCP
us-east4 ↔ AWS us-east-1 measured ~2.7 ms, so **moving everything to Northern Virginia**
(API/worker/Cloud Tasks in us-east4, Neon + Upstash primary in AWS us-east-1) is the
recommended next infra step. **Not approved yet** — ask the owner. Details and re-run
instructions: `backend/docs/LATENCY_AND_ARCHITECTURE.md`, `backend/scripts/latency_probe.py`.

## 3. Request architecture (backend)

**In the request (respond with the result):** anything that only needs Postgres or
Redis: login, registration, profile, tokens, verification, **payments** (Bachs checkout
and verification run in the request so the user is activated before the response).

**Queued for the worker (respond immediately):** anything slow or external — emails,
FCM pushes, exports, purges, gate access logs, visitor-token state, chat persistence.

- Producers enqueue, then call `wake_worker()` (`app/services/worker_wake.py`), which
  creates a Cloud Tasks task → worker `POST /internal/drain` (`app/worker_app.py`).
  A 5 s Redis flag collapses bursts; the worker drains everything due, schedules its
  own next wake for future retries, then sleeps.
- Notifications use `notification_queue.dispatch(kind, ...)`
  (`app/services/notification_queue.py`): kinds `push`, `push_many`, `email_alert`,
  `chat_push`, `settings_sync` (`staff_welcome` is only read for items queued by older
  releases). Without a worker configured (local/tests) they are sent before the
  request returns.
- **Every email goes through `ResendClient.send`** (`app/services/notifications.py`),
  which returns True only when Resend accepts it and logs Resend's reason otherwise.
  **Credential emails** (temporary passwords) share one template,
  `send_credential_email(kind, ...)`, and are sent only from durable Postgres jobs:
  - `account_password_resets` (table `account_password_reset_jobs`, column `kind`):
    `staff_welcome` (officer registered) and `reset` (directory or Security staff page
    reset). The temporary password becomes the account password **only after Resend
    accepts the email**; failures retry with backoff (up to hourly) indefinitely.
  - `landlords` (table `landlord_onboarding_jobs`): host import welcome
    (`host_welcome`), which also creates the account and keeps per-row import status.
  Never send a credential email through `notification_queue` (it drops after 3 tries).
- **Never** do slow work inline in a request, and **never** use `asyncio.create_task` /
  `BackgroundTasks` for external calls: with request-only CPU they stall after the
  response. The owner explicitly rejected processing jobs inline ("SERVERLESS_MODE").
- Durable jobs (SOS deliveries, welcome emails, password resets, history jobs) live in Postgres tables
  with leases and `SKIP LOCKED`; Redis lists carry access logs, token state, chat and
  notifications. Queue drains must skip or repair rows referencing deleted users so
  one bad item cannot block a queue.
- bcrypt always runs off the event loop (`get_password_hash_async`,
  `verify_password_async`).

## 4. Redis caching (cache-aside)

Read Redis → on miss or TTL expiry read Postgres and re-cache → **delete the entry right
after every create, update or delete commit**.

| Data | Key | TTL | Invalidated by |
|---|---|---|---|
| Signed-in profile (auth + `GET /users/me/profile`) | `residence:user:{app_id}:{email}` | 30 min | every account creation path, profile/password/status/role change, deletion, resend-welcome |
| Resident visitor list (`GET /tokens/visitor/history`) | `residence:visitor_history:{app_id}:{user_id}` | 5 min | token or book-out creation |
| Token live status (check-in/out) | `residence:token_live_status:{token_id}` | 24 h | written on each gate event; overlaid with one MGET |

Build the profile entry only with `user_cache_payload()` in `app/core/redis.py`; bump
`USER_CACHE_VERSION` when its shape changes.

## 5. Accounts, auth and naming

- **Password rule: minimum 8 characters, maximum 72 UTF-8 bytes, everywhere** (backend
  `PASSWORD_MIN_LENGTH` in `app/services/auth.py`, mobile screens, Admin forms).
- Estate sign-in matches email case-insensitively (exact match preferred; never guesses
  between two accounts differing only by case). Lockout counts all capitalisations.
- An estate account has one active session across mobile and Admin. A successful
  sign-in replaces the previous session and clears its push registration. Profile
  and password changes preserve the active session while rotating its access token.
  PostgreSQL is authoritative for session checks, including chat WebSockets.
- **Platform owner** is created on container start from `PLATFORM_OWNER_EMAIL` /
  `PLATFORM_OWNER_PASSWORD` (secret) / `PLATFORM_OWNER_NAME` if missing; an existing
  account is never modified (`app/services/owner_bootstrap.py`). It is a separate
  `GLOBAL` account even if an estate login uses the same email.
- **Security officers** are registered only by caretakers on the Admin *Security staff*
  page (`POST /api/v1/security-staff`). They get a temporary password (shown once to
  the caretaker, emailed via the `account_password_resets` job) that starts working only
  once the email is accepted, and are blocked from every endpoint until they set their
  own. A reset works the same way: the current password keeps working until the reset
  email is accepted. The page shows each officer's email status.
  Caretakers edit officers with `PATCH /api/v1/security-staff/{id}`: name, sign-in email,
  phone, plus `security_staff_profiles` (staff ID, alternate phone, home address,
  emergency contact, notes). Every change goes to the admin audit log. A changed email
  signs the officer out; if they never signed in, a new welcome goes to the new address.
  Suspend/reinstate via `PATCH /users/{id}/status`; reset requires a confirmation dialog.
- Estate directory caretakers can request a generated password reset on another
  estate account. Postgres stores a job ID and encrypted temporary password. The
  worker retries delivery and activates the temporary password only after provider
  acceptance. Sign-in then requires a password change. The directory only displays
  the job reference and delivery status.
- Moving a resident directory account into Archive suspends access and removes it
  from the active directory. A caretaker can reinstate the original account within
  30 days. A retention hold keeps the record archived and releases its email for
  a separate registration. Same-estate email matches appear as returning users
  in the directory. Reinstatement does not merge two accounts.
- The worker purges unheld archived accounts and their linked records after 30
  days. It checks for retention holds under the account lock. Sign-in surfaces
  can request reinstatement by estate App ID and email. The request queues an
  email and records a notice for active estate caretakers. It does not grant access.
- User-facing wording is **"Host / Property Owner"**; the stored role value, API paths
  (`/api/v1/landlords`) and DB tables keep `landlord` — do not rename those.

## 6. Mobile app structure (one install, two separate apps)

The resident and gate (security) experiences stay separate inside one install. The
owner approved a shared chat implementation so fixes to the inbox and thread apply
to both roles while each app keeps its own navigation and presentation.

```
lib/
  main.dart          bootstrap; after sign-in picks ResidentApp or GateApp by role
  shared/            sign-in, app-wide pieces, and common chat
    auth/ config/ models/user_model.dart notifications/ sos/ account/ chat/ ui/
  resident/          resident chat entry point, screens, state (visitor_store), data, models, widgets
  gate/              guard chat entry point, screens, state (gatehouse_store, gate_visitor_store),
                     data, models
```

- `gate/` must never import `resident/`; `resident/` never imports `gate/`; `shared/`
  imports neither. Enforced by `mobile/test/module_boundaries_test.dart`.
- Common inbox and conversation UI, message and composer rendering, refresh and
  server requests live in `lib/shared/chat/`. Resident and gate chat entry points
  provide the participant labels, role, navigation, and any role-specific inbox
  presentation. Preserve the server's authorization checks.
- Keep other code module-local unless both apps genuinely need it. Keep
  `GateVisitorStore` and the gate `visitor_pass` and `visitor_api` in `lib/gate/`.
- **Gate-specific redesign work happens in `lib/gate/`.** Changes to common chat
  belong in `lib/shared/chat/` when they apply to both roles.

## 7. How to test and deploy

- **Backend tests** (never against Neon): throwaway containers on a private network,
  `.env` masked:
  `docker run postgres:15-alpine` + `redis:7-alpine`, create `residence_test`, then run
  `backend-backend` image with `-v backend:/app -v empty.env:/app/.env`,
  `DB_URL=postgresql://rp:rp@<pg>:5432/residence`, `REDIS_URL=redis://<redis>:6379/0`,
  `python -m pytest -q`. Known pre-existing failure:
  `test_chat_audit_unit::test_removing_chat_account_revokes_instead_of_deleting_history`
  (stale mock for the owner's `delete_user` rewrite).
- **Mobile**: `flutter analyze` and `flutter test` in `mobile/`.
- **Admin**: `npx tsc --noEmit` in `Admin/`.
- **Deploy backend** (same image for both): `gcloud builds submit backend --region
  us-east5 --tag us-east5-docker.pkg.dev/residence-elastrocloud/cloud-run-source-deploy/residence-api:<tag>`,
  then `gcloud run deploy residence-worker --image ...` and
  `gcloud run deploy residence-api --image ...` (region us-east5). Entrypoint runs
  migrations (API only; worker has `SKIP_MIGRATIONS=1`) and the owner bootstrap.
- **Deploy Admin**: `npm run deploy` in `Admin/` (wrangler; vars in `wrangler.jsonc`).
- **Run the mobile app**: `python tool/dev_run.py --device <serial>` in `mobile/`
  (live API, hot reload on save; `R` + Enter = hot restart).
- Emulator `Medium_Phone_API_37.0` uses `hw.gpu.mode=host` on the NVIDIA GTX 1650.
- Agents may **not** type passwords into the live backend; the owner signs in. A
  `residence-api-staging` service with its own database would allow agent sign-ins.

## 8. Open items (ask before acting)

1. Move all services to Northern Virginia (see section 2).
2. Stale `delete_user` unit test (section 7).
3. Security officer "Peter James" was registered with `@gmaill.com` (typo).
4. Merge Admin and mobile branches into `main` when the owner approves.
5. Staging environment for agent-driven end-to-end testing.
