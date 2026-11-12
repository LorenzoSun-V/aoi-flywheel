-- 0009: the record of who did what, and three roles for people.

-- Roles: admin, algo, field (and worker, the account of a GPU machine). The deploy role is merged into field:
-- a field account reads datasets and models, downloads their files and registers releases.
UPDATE users SET role = 'field' WHERE role = 'deploy';
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'algo', 'field', 'worker'));

-- Events: one row for everything an account changes (a version, a model version, a tag, a release, a job
-- submitted or cancelled, an account or a token), written in the transaction that makes the change, so there
-- is no change without its row and no row without its change. Rows are only ever added.
--   actor_id  the account; NULL when it was done with fw-server on the server itself
--   actor     the account's name at that time (or 'fw-server'), so the record reads the same later
--   action    <what>.<verb>, e.g. version.create, job.cancel, user.role
--   object    what it was done to, as people write it: SES-TX@3, det-ses#2, 任务 17, an account name
--   detail    the particulars, e.g. {"from": "algo", "to": "field"}
CREATE TABLE events (
  id        bigserial PRIMARY KEY,
  at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor_id  bigint REFERENCES users(id),
  actor     text NOT NULL CHECK (actor <> ''),
  action    text NOT NULL CHECK (action ~ '^[a-z_]+\.[a-z_]+$'),
  object    text NOT NULL,
  detail    jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX events_action ON events (action, id);
CREATE INDEX events_actor ON events (actor_id, id);

CREATE FUNCTION guard_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'events rows cannot be changed or removed';
END $$;
CREATE TRIGGER events_guard BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION guard_event();
CREATE TRIGGER events_truncate BEFORE TRUNCATE ON events
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
