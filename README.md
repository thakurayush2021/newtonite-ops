# Newtonite Ops: operational work, coordinated

A full-stack internal web app for creating, owning, approving and auditing operational work items across teams.

- **Backend:** Python 3.10+ / Flask / SQLite (WAL, FTS5). Only dependency: `Flask`.
- **Frontend:** dependency-free vanilla JS/CSS in `app/static/`, served by the same Flask process (no build step).
- **Async:** transactional-outbox job worker (runs as a thread in the app, or standalone).

## Run it

> Using VS Code? See **OPEN_IN_VSCODE.md** (F5 run/debug and test configs are included in `.vscode/`).

```bash
pip install -r requirements.txt
python run.py                      # seeds a demo DB on first run, serves http://localhost:5000
```

Sign in with any demo user, password `password123` (buttons on the login screen fill them in):

| user | role |
|---|---|
| sam | member (Support) |
| priya | lead (Support), member (Engineering) |
| maya | member (Support + Payments) |
| ethan | lead (Engineering) |
| vik | viewer (Support): read-only |
| ada | platform admin + team admin everywhere |

Open two browsers as two different users to see claim races, stale-edit conflicts and live updates.

Options: `PORT=8080`, `DATABASE=path.db`, `SEED_ITEMS=20000` (try the app at scale),
`python -m app.seed --db x.db --items 30000 --reset`, and `python -m app.worker` to run an extra standalone job worker.

## Tests

```bash
python -m unittest discover -s tests -t . -v      # 52 tests, ~2s, no extra dependencies
```

Tests target the dangerous behaviours: simultaneous claims, duplicate submissions, stale writes, authorization bypass
(including direct API calls and cross-team access), workflow bypass, DB-level invariants, async failure/redelivery/crash,
and pagination stability. I verified they have teeth by mutation: removing the `assignee_id IS NULL` guard or the
`version = ?` guard makes the relevant tests fail.

## Layout

```
app/schema.sql     tables, indexes, FTS5, append-only triggers, CHECK invariants
app/authz.py       role model and permission predicates (single source of truth)
app/workflow.py    pure state machine
app/services.py    all writes: CAS updates, events, outbox
app/queries.py     all reads: scoped listing, search, dashboard, change feed
app/api.py         HTTP, auth, idempotent write wrapper
app/worker.py      outbox job runner + notification handlers
app/static/        UI (index.html, app.js, style.css)
tests/             unittest suites
ENGINEERING_DECISIONS.md   the five+ key decisions and trade-offs
```

## The three+ "more than CRUD" behaviours

1. **Simultaneous claim:** exactly one winner; losers get a 409 carrying the current state so the UI reconciles.
2. **Duplicate/retried requests:** `Idempotency-Key` replays the stored result; the UI retries network failures with the same key.
3. **Stale information:** optimistic versioning (`expected_version`) -> 409 with current item + who changed it; edit forms are pinned to their base version.
4. **Workflow rules:** state machine, approval cannot be skipped, no self-approval, DB CHECK constraints as a backstop.
5. **Resource-level authorization:** per-team roles, per-field rules, 404 for non-members.
6. **Reliable async:** outbox, leases, backoff, dead-lettering, idempotent handlers.

## Known limitations

- **Frontend was syntax-checked and served end-to-end, but not driven in a real browser by automated tests.** Expect small
  UI rough edges; the API behaviour behind it is what the test suite covers.
- SQLite = single writer. Fine for the stated scale on one node; see decisions doc for the Postgres migration path.
- Live updates use 5s polling of a cheap change feed, not WebSockets/SSE.
- The dashboard aggregate (~65 ms at 30k items) is computed on demand; it is only refetched when the feed reports changes.
- No total counts on lists (keyset pagination, by design). No virtualised list: DOM grows as you "load more".
- Multi-team "sort by updated" needs a sort over a covering index (~25 ms at 15k visible rows); single-team streams from the index.
- Auth is minimal: bearer sessions, no login rate-limiting, no SSO/MFA, no user-management UI (team membership API exists; creating users/teams is via seed/DB).
- Notifications are in-app only; email/Slack delivery would be another job handler.
- Mentions, attachments, watchers, SLAs, bulk actions and saved views are not built.
- Removing a member releases their active items but keeps ownership of `awaiting_approval` items (DB invariant); a lead must resolve those.
- Timestamps are UTC; due dates entered as a date mean 23:59:59 UTC that day.
