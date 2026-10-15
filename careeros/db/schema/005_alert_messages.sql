-- Gmail messages already read for job links (Gmail's opaque message id only; the email itself is never stored).
CREATE TABLE alert_messages (
  message_id TEXT PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('gmail')),
  processed_at TEXT NOT NULL,
  links INTEGER NOT NULL
);
CREATE TRIGGER alert_messages_no_update BEFORE UPDATE ON alert_messages BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
