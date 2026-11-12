-- 0010: labelling batches. A batch is a folder of images that goes through labelling and review before it
-- enters a data pool: uploaded (every upload is a frozen revision), sent to a reviewer, rejected with a
-- comment or approved, and then added to the pool by an algorithm engineer.
--
-- A batch's revisions are the versions of a dataset of kind 'batch' that belongs to it, so everything that
-- works on a version works on a revision: fw get, the web's image browser, the diff, fw-server verify,
-- prediction jobs. A revision's file list holds paths inside the batch's folder, which is exactly the file
-- list that adding a folder to a pool takes (changes.py), so nothing is copied or uploaded again at import.

ALTER TABLE datasets DROP CONSTRAINT datasets_kind_check;
ALTER TABLE datasets ADD CONSTRAINT datasets_kind_check
  CHECK (kind IN ('pool', 'test', 'regression', 'other', 'trainset', 'batch'));

-- status: open      a revision is uploaded, not sent to review yet
--         review    waiting for the reviewer's answer on the newest revision
--         rejected  the reviewer sent it back; the owner uploads another revision
--         approved  the newest revision may be added to the pool
--         imported  it is in the pool (imported_version_id); the end
--         closed    given up; the end. Nothing is ever deleted.
CREATE TABLE batches (
  id                   bigserial PRIMARY KEY,
  dataset_id           bigint NOT NULL UNIQUE REFERENCES datasets(id),   -- holds the revisions; its name is the batch's
  pool_id              bigint NOT NULL REFERENCES datasets(id),          -- where it goes when approved
  dir                  text COLLATE "C" NOT NULL CHECK (dir <> '' AND dir NOT LIKE '/%' AND dir NOT LIKE '%/'),
  owner_id             bigint NOT NULL REFERENCES users(id),
  status               text NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'review', 'rejected', 'approved', 'imported', 'closed')),
  reviewer_id          bigint REFERENCES users(id),                      -- the reviewer last asked
  imported_version_id  bigint REFERENCES versions(id),                   -- the pool version it went into
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (dataset_id <> pool_id),
  CHECK ((status = 'imported') = (imported_version_id IS NOT NULL)),
  CHECK (status NOT IN ('review', 'approved', 'imported') OR reviewer_id IS NOT NULL)
);
CREATE INDEX batches_pool ON batches (pool_id);
CREATE INDEX batches_reviewer ON batches (reviewer_id) WHERE status = 'review';

-- What a revision adds to its version: who labelled it (a person's name as typed: one account may be shared
-- by several people on a line; the account is versions.created_by).
CREATE TABLE batch_revisions (
  version_id  bigint PRIMARY KEY REFERENCES versions(id),
  batch_id    bigint NOT NULL REFERENCES batches(id),
  labeler     text NOT NULL CHECK (labeler <> '' AND length(labeler) <= 64),
  by_reviewer boolean NOT NULL DEFAULT false   -- uploaded by the reviewer together with the approval
);
CREATE INDEX batch_revisions_batch ON batch_revisions (batch_id);

-- The course of a batch, one row per step, only ever added: who did what on which revision, and what the
-- reviewer wrote.
CREATE TABLE batch_steps (
  id           bigserial PRIMARY KEY,
  batch_id     bigint NOT NULL REFERENCES batches(id),
  at           timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor_id     bigint NOT NULL REFERENCES users(id),
  step         text NOT NULL CHECK (step IN ('upload', 'submit', 'reject', 'approve', 'import', 'close')),
  version_id   bigint REFERENCES versions(id),      -- the revision it is about
  reviewer_id  bigint REFERENCES users(id),         -- submit: who is asked
  self_review  boolean NOT NULL DEFAULT false,      -- submit: the owner asked themselves
  comment      text NOT NULL DEFAULT '',
  images       jsonb NOT NULL DEFAULT '[]',         -- reject: the images the reviewer points at
  pool_version_id bigint REFERENCES versions(id),   -- import: the pool version made
  CHECK (step <> 'reject' OR comment <> ''),
  CHECK ((step = 'submit') = (reviewer_id IS NOT NULL)),
  CHECK ((step = 'import') = (pool_version_id IS NOT NULL))
);
CREATE INDEX batch_steps_batch ON batch_steps (batch_id, id);

-- A batch keeps its dataset, pool, folder and owner; a finished one (imported, closed) never changes again;
-- rows are never removed.
CREATE FUNCTION guard_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'batches rows cannot be removed';
  END IF;
  IF NEW.id <> OLD.id OR NEW.dataset_id <> OLD.dataset_id OR NEW.pool_id <> OLD.pool_id OR NEW.dir <> OLD.dir
     OR NEW.owner_id <> OLD.owner_id OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'batch %: its dataset, pool, folder and owner cannot change', OLD.id;
  END IF;
  IF OLD.status IN ('imported', 'closed') THEN
    RAISE EXCEPTION 'batch %: it is % and cannot change', OLD.id, OLD.status;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER batches_guard BEFORE UPDATE OR DELETE ON batches
  FOR EACH ROW EXECUTE FUNCTION guard_batch();
CREATE TRIGGER batches_truncate BEFORE TRUNCATE ON batches
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

-- A batch's dataset is of kind 'batch' and its pool is not a snapshot or another batch.
CREATE FUNCTION guard_batch_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM datasets WHERE id = NEW.dataset_id AND kind = 'batch') THEN
    RAISE EXCEPTION 'batch: dataset % is not of kind batch', NEW.dataset_id;
  END IF;
  IF EXISTS (SELECT 1 FROM datasets WHERE id = NEW.pool_id AND kind IN ('trainset', 'batch')) THEN
    RAISE EXCEPTION 'batch: dataset % is a snapshot or a batch, not a pool', NEW.pool_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER batches_insert_guard BEFORE INSERT ON batches
  FOR EACH ROW EXECUTE FUNCTION guard_batch_insert();

-- A revision row is written once, for a version of the batch's own dataset that is not frozen yet.
CREATE FUNCTION guard_batch_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION 'batch_revisions rows cannot be changed or removed';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM versions v JOIN batches b ON b.dataset_id = v.dataset_id
                 WHERE v.id = NEW.version_id AND b.id = NEW.batch_id AND NOT v.frozen) THEN
    RAISE EXCEPTION 'version % is frozen or not a revision of batch %', NEW.version_id, NEW.batch_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER batch_revisions_guard BEFORE INSERT OR UPDATE OR DELETE ON batch_revisions
  FOR EACH ROW EXECUTE FUNCTION guard_batch_revision();
CREATE TRIGGER batch_revisions_truncate BEFORE TRUNCATE ON batch_revisions
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

-- Every version of a batch dataset is a revision: it cannot freeze without its batch_revisions row.
CREATE FUNCTION guard_batch_version_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.frozen AND NOT OLD.frozen
     AND EXISTS (SELECT 1 FROM datasets WHERE id = NEW.dataset_id AND kind = 'batch')
     AND NOT EXISTS (SELECT 1 FROM batch_revisions WHERE version_id = NEW.id) THEN
    RAISE EXCEPTION 'version %: a version of a batch must be recorded as a revision before it freezes', NEW.id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER versions_batch_freeze BEFORE UPDATE ON versions
  FOR EACH ROW EXECUTE FUNCTION guard_batch_version_freeze();

CREATE FUNCTION guard_batch_step() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'batch_steps rows cannot be changed or removed';
END $$;
CREATE TRIGGER batch_steps_guard BEFORE UPDATE OR DELETE ON batch_steps
  FOR EACH ROW EXECUTE FUNCTION guard_batch_step();
CREATE TRIGGER batch_steps_truncate BEFORE TRUNCATE ON batch_steps
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
