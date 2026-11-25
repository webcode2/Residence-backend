# Landlord and household onboarding

Caretaker → CSV/Excel queue → landlord welcome email → mandatory password change → landlord invitation → family/tenant registration.

## Caretaker dashboard

Open Resident directory. Bulk Import accepts CSV, XLSX and XLS, up to 5 MB and 500 landlords per file. Required columns are Name, Email, Phone and House Number; Street is optional. The downloadable template includes the supported headings. Save phone and house identifiers as text in Excel to preserve leading zeros. All rows are validated before jobs are created. Manual landlord additions use the same queue.

The onboarding tracker lists job/batch IDs, house, email, state, attempts, next retry and a safe failure explanation. Refresh retrieves current progress without continuous client polling. It shows 100 jobs per page. Failed/retrying jobs have a Retry action. Duplicate emails do not recreate accounts or reset passwords. Caretakers can access only their estate's jobs.

States: queued → processing → sent; transient failures → retrying → processing; after five unsuccessful attempts → failed. A manual retry starts a new attempt cycle. Existing accounts are skipped. “Sent” means the email provider accepted the message, not confirmed inbox delivery. Provider bounce/delivery webhooks are not implemented in this change.

## Durable processing

`landlord_onboarding_jobs` in PostgreSQL is the persistent queue. Queue consumers run in the dedicated `python worker.py --daemon` process, or in API processes when `ENABLE_EMBEDDED_WORKERS=true` (simple/dev single-process mode). Consumers claim rows with SKIP LOCKED, use a two-minute processing lease, and reclaim expired leases after crashes. Per-job processing locks prevent concurrent email attempts. Failed attempts back off exponentially; no API request waits for email delivery.

The worker creates one landlord account with a random temporary password. Pending email credentials are encrypted using a domain-separated key derived from SECRET_KEY. Password ciphertext is never returned by APIs and is cleared after provider acceptance. Retries reuse the password and stable Resend idempotency key. Resend's deduplication window is provider-controlled; an exceptionally delayed retry can deliver the same email again, but it will not create another account or rotate the password. Drain pending jobs before rotating SECRET_KEY, or re-encrypt pending credentials under the new key.

Configure RESEND_API_KEY, RESEND_SENDER_EMAIL and RESEND_SENDER_NAME with a verified sender. Missing credentials cause visible retry/failure, never simulated welcome-email success. Real outbound delivery requires the configured provider; tests mock it and send no real emails.

## First login and invitations

Imported landlords receive their email, estate App ID and temporary password. The login response carries must_change_password and a restricted 30-minute token. Normal API calls and chat WebSockets reject that account/token until `/api/v1/auth/change-password` validates the old password, requires a different password of at least 12 characters (maximum 72 UTF-8 bytes), clears the requirement and replaces/invalidates the restricted token. The mobile app shows the password form before the estate screens.

Landlords open Profile → Family & tenant invitations. Each invitation is for family or tenant, expires in seven days and admits one new account. The backend derives the host ID and address from the authenticated landlord, ignoring client-supplied addresses. Landlords can copy/revoke their invitations and see registered household members. Caretakers can inspect the landlord's occupants in the directory. Every invited account retains landlord_id, invitation_id, relationship_type and inherited house/street. Deleting a host with household/onboarding history revokes the account rather than breaking attribution.

Public registration requires the estate App ID and an unused, unrevoked, unexpired invitation from an active, onboarded landlord. Redemption locks the invitation until commit. Existing accounts cannot be relinked through public registration. The invited user always receives the resident role regardless of submitted role fields. Caretakers cannot bypass the queue by directly creating/promoting a landlord through the users API.

## Deployment and verification

Apply migration `fa648192a3b5` after `ef53708192a4` before starting this backend version. It adds nullable profile/attribution fields, first-login state and the queue table. Existing user passwords/accounts are preserved; existing invitation codes receive a seven-day expiry grace period. Rebuild/install backend requirements for openpyxl and xlrd. Restart backend consumers, rebuild the admin dashboard, and distribute the updated mobile build together.

Use `tests/test_onboarding.py` for import validation, retry recovery, secret redaction, restricted login, single-use/concurrent invitation redemption and estate isolation. Actual inbox delivery and installed-device onboarding must also be checked in a configured release environment. No live database migration or real welcome-email delivery is implied by a passing isolated test run.

## Verification recorded on 23 October 2026

- 68 backend tests passed: 64 covering onboarding, users, welcome notifications, authentication, guard verification, entry/exit, chat and SOS, plus four directory/gate regressions.
- 8 mobile tests passed, including first-login screen gating and password form validation.
- Dashboard TypeScript check and production build passed.
- Flutter analyzer reports only two existing const-constructor suggestions in gatehouse_history_screen.dart; no onboarding analyzer issues.
- Full migration chain through fa648192a3b5 passed on the newly created isolated residence_onboarding_migration_demo_20261023 database. The former named seed demo database was absent; no production migration was attempted.

Live rollout remains pending. Apply the migration and release backend/dashboard/mobile changes together, with a configured verified email sender. No real welcome emails were sent during tests.

## Saved original spreadsheets

New accepted CSV/XLSX/XLS uploads are stored in PostgreSQL `landlord_import_files`, including the exact original bytes, safe original filename, size, estate, uploader, timestamp and import batch. Files are private and accessible only through authenticated caretaker endpoints for the same estate. List responses omit the file contents. This storage survives application/container restarts and uses the database backup policy; no container-local upload directory or extra public URL is used.

Resident directory → onboarding tracker → Saved import files provides Download and Delete file. There is no automatic expiry. Delete becomes available when the batch has no queued, processing or automatically retrying jobs. Sent, skipped and terminal failed jobs count as finished. The confirmation removes original bytes from active storage and records the deletion time; accounts and extracted job data/history remain. A failed job can still be manually retried after its original file is deleted. Existing backups follow their normal retention schedule.

Invalid/rejected uploads are not retained. Files processed before this retention feature cannot be recovered from extracted rows. Manual landlord additions have no source file. Apply migration `ab7592a3b4c6` after `fa648192a3b5` before deploying this update; production migration remains pending until rollout.

Saved-file endpoints (all estate-caretaker authenticated):

- `GET /api/v1/landlords/import-files`: paginated metadata and processing/deletion eligibility; no original bytes.
- `GET /api/v1/landlords/import-files/{id}/download`: original file as a no-store attachment.
- `DELETE /api/v1/landlords/import-files/{id}`: remove the original after processing; keep job/account history.

The retention tests cover exact original downloads, active-job deletion protection, tenant/role isolation, safe filenames, deletion preserving accounts and job history, and failed-job retries after deletion.

Saved-file follow-up verification: 21 related backend tests passed; dashboard TypeScript and production build passed. Migration ab7592a3b4c6 passed against the isolated migration demo database. Live rollout remains pending.
