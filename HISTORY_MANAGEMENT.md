# Estate access history and verification

History reads, exports and confirmed deletions are estate-scoped. This change concerns gate/access history and visitor-token lifecycle. Conversations, SOS alerts, onboarding history and saved upload originals keep their separate policies; deleting access history does not delete those records or accounts.

## Gate verification

Redis remains the primary cached lookup/one-use claim path. Database fallbacks use partial indexes containing live visitor records, rather than scanning accumulated expired tokens. Entry requires pending/unused/unexpired state; exit-code lookup requires checked-in state and an unexpired book-out code. Original-entry-code exit fallback ignores terminal records and selects one current candidate deterministically. Checked-in visitors are never removed by retention cleanup, even if they arrived before the retention cutoff.

History stays in separate tables in the same PostgreSQL database. This avoids adding a second database before evidence justifies its operational cost. It reduces unnecessary work but does not physically isolate CPU/disk resources. No end-to-end O(1) claim is made. Redis key lookups, database index lookups, notification dispatch, queue operations and network time are separate costs.

## Plan retention

The effective value is the estate subscription's `log_retention_days`, including estate overrides; defaults are 30/90/180/365. The shared policy helper consistently uses 30 days when a subscription is absent. Editing a catalogue plan does not retroactively rewrite every estate subscription.

Interactive access history and exports respect the estate cutoff immediately. Scheduled maintenance removes expired access logs in batches of 1,000, at most ten batches per estate per sweep, committing each batch. Cleanup can therefore lag behind a large downgrade; expired rows remain hidden while the bounded backlog drains. A renewed, owner-checked Redis lease prevents overlapping scheduled sweeps and stops a worker that loses ownership.

Visitor expiration and stale exit-code updates are limited to 1,000 records per sweep. Terminal visitor rows (`expired` or `checked_out`) are removed in batches only outside the estate retention window; recent checkouts and all checked-in visitors remain. Visitor-history reads apply retention while retaining active visitors. Active tokens are never removed by the manual access-log purge.

## Reading and exporting

The caretaker dashboard uses signed, tenant/filter-bound keyset cursors ordered by `(created_at, id)`. Date and status filters apply before pagination. Deep offset browsing is replaced with Next/Previous cursors; the legacy endpoint accepts offsets only up to 1,000. New indexes support the estate/date cursor and estate/status/date cursor.

CSV exports are queued database jobs, processed in 1,000-record chunks. Each chunk and its progress cursor commit atomically, so a restarted worker or manual retry resumes without duplicating committed rows. UTF-8 CSV files open in Excel; formula-like cell values are neutralized. Downloading streams one persisted chunk at a time through an authenticated dashboard route, rather than loading the entire file into a server action. CSV is supported; a separate native XLSX exporter is not included.

Exports use an event-time upper boundary. As with the live feed, events still waiting in the ingestion queue may not be present in an export; this is not a database-wide repeatable-read snapshot. Artifacts are separately retained for 24 hours and inaccessible immediately on expiry, plan-retention reduction, or explicit purge invalidation. Blob reclamation is bounded. An exported/downloaded local file is outside server retention control.

## Confirmed deletion

1. Select a cutoff and, optionally, an applied verification-status filter.
2. Queue a read-only purge preview. Counts are built in bounded background steps.
3. Refresh jobs, review the affected range/count and type `PURGE` in the confirmation dialog.
4. Only the caretaker who requested that preview can confirm it, within 15 minutes of completion.
5. The worker deletes matching access logs in batches and records the actual final count. Late matching events can make this differ from the preview.

A preview never deletes records. Confirming invalidates earlier exports for the estate before deleting source rows. Confirmed ranges are also retained as tombstones: the access-log ingestion worker and synchronous database fallback suppress late queued events in those ranges so purged history cannot reappear. Batch ingestion is serialized with purge confirmation/deletion using short estate-row locks; cached gate requests continue to enqueue without taking those locks.

Request, confirmation, completion, failure, retry and automatic retention cleanup are recorded in admin audit history with actor/estate/range/count as applicable. Jobs retry transient failures up to five times; failed jobs can be retried manually. Jobs and audit metadata remain after deleting source logs. All downloads, previews, confirmations and job management require a caretaker of the same estate; guards retain allowed history reading but cannot export/purge it.

## Deployment

Apply `bc86a3b4c5d7` (history job/chunk tables) and `cd97b4c5d6e8` (concurrent indexes) after `ab7592a3b4c6`, then deploy the backend and dashboard. Index DDL is separate and resumable; invalid interrupted indexes are dropped/rebuilt on retry. Every API worker starts a durable history-job consumer. A PostgreSQL transaction-scoped advisory lock permits only one queued export/purge chunk globally at a time. Consumers rotate through jobs by their latest update, throttle between chunks, and use row locks/atomic commits for crash recovery. This bounds background concurrency on the primary while allowing failover between API workers. Automated retention requires ENABLE_MAINTENANCE_SCHEDULER; its default interval is hourly.

Do not drop a shared monthly partition simply because one estate has a shorter retention window. If partitioning becomes justified by measured volume, partition by date for query pruning and retain per-estate cleanup until the entire partition is expired for all affected estates. A separate reporting instance is a later option if measured resource contention persists.

## Verification

`tests/test_history_jobs.py` covers retention differences, stable cursor navigation, cursor tampering/tenant isolation, export chunking/formula safety/expiry, explicit purge confirmation, preview expiry, cross-estate protection, retry recovery, preservation of active visitors, late-event suppression and maintenance-lease ownership.

The query-plan test inserts 20,000 expired visitor records sharing the queried code plus one live record, and 20,000 additional history rows. It runs ANALYZE and EXPLAIN (ANALYZE, BUFFERS) without disabling sequential scans or forcing indexes. Evidence is saved to `/tmp/residence-history-query-plans.json`; timings are isolated, local, warm-cache SQL measurements, not production request-latency guarantees.

Production-configured data is not migrated or purged during verification. Deployment and realistic concurrent load/soak testing remain separate rollout steps.

Verification recorded on 23 October 2026: 34 targeted backend tests passed, including gate/Redis-drain/directory regressions; the dashboard TypeScript check and production build passed. Both migrations passed on `residence_onboarding_migration_demo_20261023`. The saved representative query plans are in [docs/history-query-plans.json](docs/history-query-plans.json). The local production-configured database was neither migrated nor purged as part of this work; rollout remains pending.
