-- GitHub issues read from the private recommendations inbox (e.g. job lists posted by a ChatGPT scheduled task).
-- Only the issue number, how many links it had and the reply CareerOS posted are kept; jobs and leads go to their
-- own tables. `closed` lets a run that processed an issue but failed to close it retry the close without re-reading.
CREATE TABLE inbox_issues (
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  processed_at TEXT NOT NULL,
  links INTEGER NOT NULL,
  reply TEXT NOT NULL,
  closed INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0, 1)),
  PRIMARY KEY (repo, number)
);
CREATE TRIGGER inbox_issues_no_delete BEFORE DELETE ON inbox_issues BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
