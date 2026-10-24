# SOS controls and mobile notification inbox

## Behavior

- Caretaker dashboard: `/dashboard/sos`. History is limited to the authenticated estate and its subscription's log retention period.
- Caretakers can disable and restore an alert sender's SOS sending. A reason is required, and each decision is recorded in `admin_audit_logs`.
- The sending restriction is checked against the database under the sender lock. It does not revoke login or prevent receiving other estate alerts.
- Restrictions can be restored from the restricted users list even after the original alert falls outside history retention.
- Mobile notifications use a durable per-account inbox, including recipients without a registered device. FCM is a delivery signal, not the unread-count source of truth.
- The mobile bell shows unread notifications (`99+` visually, with the actual count in its accessibility label), and opens the inbox. Individual reads and mark-all-read persist on the server.
- Reading a chat clears that reader's notifications for the thread. Opening an OS chat/SOS notification marks the corresponding event read.
- Refresh occurs on sign-in, app resume, FCM events and manual refresh. There is no client interval polling.
- Offline SOS history is opened as a report without ringing. The live SOS delivery window remains 40 seconds; foreground sirens remain capped at 30 seconds.

## Alarm asset

The supplied `mobile/assets/sos_alarm.mp3` is 5.62 seconds long. It is looped into a 29-second, mono, 22,050 Hz, 16-bit PCM WAV at:

- `mobile/assets/sos_alarm.wav`
- `mobile/android/app/src/main/res/raw/sos_alarm.wav`
- `mobile/ios/Runner/sos_alarm.wav`

The three WAV assets are identical. The original MP3 is preserved. Background notification audio stays below iOS's 30-second custom sound limit.

## Schema and deployment

Migration `de108f6a4b21`, following `cd97b4c5d6e8`, adds:

- `sos_restrictions`: per-user estate SOS sending permissions and decision metadata.
- `app_notifications`: per-user inbox, event deduplication and read state.
- `ix_sos_alerts_estate_history`: estate/date/id history index.

No existing records or passwords are changed by the upgrade. The migration has been tested in a temporary schema in the isolated test database, including downgrade, with the transaction rolled back.

**Deployment status (2026-10-24):** user explicitly approved the local migration and deployment. Migration `de108f6a4b21` is applied to the running local `residence_db`; backend source patches are deployed and workers were gracefully reloaded. Read-only runtime checks confirmed caretaker history/restriction access (200), resident denial for caretaker endpoints (403), per-account notification endpoints (200), and health (200). Android 1.0.13 (14) was installed on the connected phone with existing app data preserved. No seed data or passwords were modified.

## Validation

- Backend regression suite: 70 passed before final additions; final SOS/chat/verification integration suite: 57 passed.
- Final SOS/FCM focused tests: 18 passed, including offline inbox and restriction isolation.
- Flutter suite: 41 passed. After the final restriction-message adjustment, the relevant SOS/bell tests passed again (3 tests).
- Flutter analyzer: no issues. Android debug APK built as 1.0.13 (14).
- Caretaker production build and TypeScript checks passed.
- No real estate-wide SOS broadcast was triggered. iOS assets/native changes have not been built on macOS.
