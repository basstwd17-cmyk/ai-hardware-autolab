CREATE TABLE IF NOT EXISTS commands (
  command_id  TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL,
  kind        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'completed', 'expired')),
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  claimed_at  INTEGER,
  completed_at INTEGER,
  ok          INTEGER CHECK (ok IS NULL OR ok IN (0, 1)),
  result      TEXT,
  error       TEXT
);

CREATE INDEX IF NOT EXISTS commands_pending
  ON commands (device_id, state, created_at);

CREATE TABLE IF NOT EXISTS snapshots (
  device_id  TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  cursor     INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),
  updated_at INTEGER NOT NULL
);
