# Engineering Decisions

Scope: one working day, so I optimised for *correct behaviour under concurrency and failure* over feature count.

## 1. One append-only event log as the history, audit trail, and change feed
Every important action (create, assign, status change, priority change, edit, approval, comment) writes a row to `events`
in the same transaction as the state change. Comments are events too, so a user sees a single timeline of "what happened and why".
DB triggers reject UPDATE/DELETE, so history can't silently disappear, even via a buggy code path.
The autoincrement event id doubles as the cursor for `GET /api/changes?since=N`, which is how browsers learn that something they're
looking at changed, without WebSockets.
*Trade-off:* the current state lives in `work_items` and history in `events` (not full event sourcing), so reads stay simple and fast;
the cost is that the two must be written together, which the transaction guarantees.

## 2. Concurrency: compare-and-swap in the write, not "read, check, write"
- **Claiming:** `UPDATE ... SET assignee_id=? WHERE id=? AND assignee_id IS NULL`. Rows-affected = 0 means you lost; the 409 contains the winner and the current item. Re-claiming your own item is a no-op (no duplicate history).
- **Editing:** every mutation carries `expected_version`; the UPDATE has `WHERE version = ?`. A stale writer gets 409 with the current item and who changed it last. Nothing is overwritten silently.
- **Duplicates:** `Idempotency-Key` header. The key lookup, the action, and the stored response share one transaction; `BEGIN IMMEDIATE` serialises writers, so two simultaneous duplicates cannot both execute. Only successes are stored (failures are safe to re-evaluate). Same key + different body -> 422.
- **Comments don't bump `version`**, so someone commenting never invalidates another person's in-progress edit.
*Why not locks?* Pessimistic locks leave work stuck behind someone who walked away. *Trade-off:* users can see a conflict; the UI handles it by keeping the draft and offering "keep my edits on top of the latest" or "discard".
SQLite's single-writer model makes this simple; the same conditional-UPDATE statements are correct under Postgres READ COMMITTED, which is why I made the write itself the guard instead of relying on the transaction alone.

## 3. Authorization: per-team roles, one set of predicates, enforced server-side
Roles per team (`viewer < member < lead < admin`), because a person can be a lead in Support and a member in Payments.
`authz.py` holds the predicates (`can_claim`, `can_edit`, `can_review`, ...) used both to **enforce** actions and to compute the `can` object sent to the UI, so the UI can't drift from the rules.
Resource-level rules: members may only edit/move items they created or hold; priority, assignment, closing and approval are lead decisions; approvers can't approve their own request (separation of duties).
Every read joins `memberships` *in SQL*, so a filter bug can't widen visibility; non-members get **404, not 403**, so item existence doesn't leak across teams. Tests hit the API directly (bypassing the UI) for all of this.
*Deliberately not built:* a global super-user who sees all items. Platform admins manage membership only.

## 4. Workflow as a pure state machine, with invariants also in the database
`workflow.py` is a pure function of (item, role, user), easy to test and reused by the UI. Key rules: you must own an item before starting it; resolving needs a resolution note; items flagged `requires_approval` can't skip `awaiting_approval`; closing/reopening is a lead action.
The most important invariants (`in_progress`/`awaiting_approval` imply an owner; awaiting approval implies a requester) are `CHECK` constraints, so even a future bug or a manual SQL fix can't create impossible states (tested).
Releasing started work returns it to `open` automatically to preserve that invariant.

## 5. Asynchronous work: transactional outbox, at-least-once, idempotent handlers
Notifications and the stale-work sweep are not done in the request. The job row is inserted **in the same transaction** as the action, so "committed" and "job exists" can't diverge. A worker claims jobs with an atomic `UPDATE ... RETURNING` plus a **lease**; a crashed worker's lease expires and the job is re-claimed. Failures retry with exponential backoff, then become `dead` (kept for inspection). Because delivery is at-least-once, handlers are idempotent: notifications are `INSERT OR IGNORE` against `UNIQUE(user_id, dedupe_key)`. Tests cover failure+retry, redelivery (no duplicates), dead-lettering without blocking others, and lease recovery.
*Why not Redis/Celery?* One less moving part for a day-one system and it gives transactional enqueue for free. The `jobs` interface is small enough to swap for SQS/Rabbit later. *Trade-off:* the DB is also the queue, so very high job volume would eventually want a real broker.

## 6. Search, scale and the UI
- **Keyset (cursor) pagination** instead of OFFSET: stable while new items arrive (tested) and flat cost deep into the list (~20 ms/page at page 150 of 30k items).
- **FTS5** for title/description (prefix matching, quoted terms so user input can't inject query syntax), plus `#id` lookup.
- **Named queues** ("Needs attention", "Waiting for my approval", "Gone quiet", ...) are SQL fragments reused for the list *and* the dashboard counts, so the badge number and the list can never disagree.
- Frontend never holds the dataset: pages of 40, sequence-numbered loads (an older response can't overwrite a newer one), optimistic UI that always reconciles with the server's answer, and polling that only refetches rows already on screen.

## What I intentionally did not build
WebSockets/SSE (polling a cheap feed is enough and simpler to operate), mentions/attachments/SLAs, user-management UI, email delivery, list virtualisation, full event sourcing.

## If the app grew 10x
Postgres (same conditional UPDATEs; `jsonb`, partial indexes, `SKIP LOCKED` for jobs), per-team denormalised counters instead of on-demand dashboard aggregates, SSE/WebSocket push fed by the event stream, a real broker for jobs, table partitioning for `events`, and search moved to a dedicated engine if relevance needs grow.
