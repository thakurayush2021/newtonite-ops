-- Newtonite Ops schema (SQLite). All CREATEs are idempotent.

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name          TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin      INTEGER NOT NULL DEFAULT 0,   -- platform admin: manages teams/membership only
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,                -- sha256(token); raw token is never stored
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

-- A user can be in many teams with a different role in each.
CREATE TABLE IF NOT EXISTS memberships (
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role    TEXT NOT NULL CHECK (role IN ('viewer','member','lead','admin')),
  PRIMARY KEY (team_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);

CREATE TABLE IF NOT EXISTS work_items (
  id                    INTEGER PRIMARY KEY,
  team_id               INTEGER NOT NULL REFERENCES teams(id),
  title                 TEXT NOT NULL,
  description           TEXT NOT NULL DEFAULT '',
  status                TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','in_progress','blocked','awaiting_approval','resolved','closed')),
  priority              TEXT NOT NULL DEFAULT 'normal'
                        CHECK (priority IN ('low','normal','high','urgent')),
  prank                 INTEGER NOT NULL DEFAULT 1,   -- numeric priority so it can be indexed/sorted
  assignee_id           INTEGER REFERENCES users(id),
  creator_id            INTEGER NOT NULL REFERENCES users(id),
  due_at                TEXT,
  requires_approval     INTEGER NOT NULL DEFAULT 0,
  approval_state        TEXT NOT NULL DEFAULT 'none'
                        CHECK (approval_state IN ('none','pending','approved','rejected')),
  approval_requested_by INTEGER REFERENCES users(id),
  resolution            TEXT,
  version               INTEGER NOT NULL DEFAULT 1,   -- optimistic concurrency token
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  last_activity_at      TEXT NOT NULL,
  -- Invariants enforced by the database, not just by application code:
  CHECK (status NOT IN ('in_progress','awaiting_approval') OR assignee_id IS NOT NULL),
  CHECK (status != 'awaiting_approval' OR (requires_approval = 1 AND approval_requested_by IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_items_team_updated  ON work_items(team_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_team_priority ON work_items(team_id, prank DESC, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_team_created  ON work_items(team_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_assignee      ON work_items(assignee_id, status);
CREATE INDEX IF NOT EXISTS idx_items_due           ON work_items(due_at)
  WHERE due_at IS NOT NULL AND status IN ('open','in_progress','blocked','awaiting_approval');
CREATE INDEX IF NOT EXISTS idx_items_activity      ON work_items(last_activity_at)
  WHERE status IN ('open','in_progress','blocked');

-- Full-text search (external-content FTS5 index kept in sync by triggers).
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, description, content='work_items', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS wi_ai AFTER INSERT ON work_items BEGIN
  INSERT INTO items_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;
CREATE TRIGGER IF NOT EXISTS wi_ad AFTER DELETE ON work_items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, description) VALUES ('delete', old.id, old.title, old.description);
END;
CREATE TRIGGER IF NOT EXISTS wi_au AFTER UPDATE OF title, description ON work_items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, description) VALUES ('delete', old.id, old.title, old.description);
  INSERT INTO items_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;

-- Append-only audit/activity log. Comments are events too, so there is ONE timeline.
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotonically increasing: doubles as the change-feed cursor
  item_id    INTEGER NOT NULL REFERENCES work_items(id),
  actor_id   INTEGER REFERENCES users(id),       -- NULL = system
  type       TEXT NOT NULL,
  data       TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_id, id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

-- Transactional outbox / job queue (written in the SAME transaction as the state change).
CREATE TABLE IF NOT EXISTS jobs (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','dead')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  run_at       TEXT NOT NULL,
  locked_until TEXT,                      -- lease; an expired lease means the worker died
  last_error   TEXT,
  dedupe_key   TEXT UNIQUE,               -- enqueue is idempotent
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, run_at);

CREATE TABLE IF NOT EXISTS notifications (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  item_id    INTEGER REFERENCES work_items(id),
  kind       TEXT NOT NULL,
  message    TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  read_at    TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (user_id, dedupe_key)            -- makes at-least-once job delivery effectively-once
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, id DESC);

-- Stored responses so a retried POST returns the original result instead of repeating the action.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  user_id       INTEGER NOT NULL,
  key           TEXT NOT NULL,
  fingerprint   TEXT NOT NULL,
  status_code   INTEGER NOT NULL,
  response_body TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);
