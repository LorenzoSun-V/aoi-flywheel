-- 0002: training-set snapshots.
-- A snapshot is a version of a dataset of kind 'trainset': its files are the laid-out tree (train/, val/, test/,
-- classes.txt, trainset.json), so freezing, fetching and verifying work exactly as for any version. The rows
-- below record how it was made; they are written in the transaction that creates the version, before it is
-- frozen, and never change afterwards.

ALTER TABLE datasets DROP CONSTRAINT datasets_kind_check;
ALTER TABLE datasets ADD CONSTRAINT datasets_kind_check
  CHECK (kind IN ('pool', 'test', 'regression', 'other', 'trainset'));

CREATE TABLE trainset_versions (
  version_id   bigint PRIMARY KEY REFERENCES versions(id),
  based_on_id  bigint REFERENCES versions(id),   -- the snapshot it continued from, if any
  classes      jsonb NOT NULL CHECK (jsonb_typeof(classes) = 'array'),  -- names; the index is the class id
  rule         jsonb NOT NULL CHECK (jsonb_typeof(rule) = 'object'),    -- how the split was made (for people)
  counts       jsonb NOT NULL CHECK (jsonb_typeof(counts) = 'object')
);

CREATE TABLE trainset_sources (
  version_id  bigint NOT NULL REFERENCES versions(id),
  position    int NOT NULL CHECK (position >= 0),
  source_id   bigint NOT NULL REFERENCES versions(id),
  PRIMARY KEY (version_id, position)
);

-- Rows may be added only for a version that is not frozen yet and belongs to a trainset; nothing is ever
-- changed or removed.
CREATE FUNCTION guard_trainset_row() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION '% rows cannot be changed or removed', TG_TABLE_NAME;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM versions v JOIN datasets d ON d.id = v.dataset_id
                 WHERE v.id = NEW.version_id AND NOT v.frozen AND d.kind = 'trainset') THEN
    RAISE EXCEPTION 'version % is frozen or not a trainset: % rows cannot be added', NEW.version_id, TG_TABLE_NAME;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trainset_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON trainset_versions
  FOR EACH ROW EXECUTE FUNCTION guard_trainset_row();
CREATE TRIGGER trainset_sources_guard BEFORE INSERT OR UPDATE OR DELETE ON trainset_sources
  FOR EACH ROW EXECUTE FUNCTION guard_trainset_row();
CREATE TRIGGER trainset_versions_truncate BEFORE TRUNCATE ON trainset_versions
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER trainset_sources_truncate BEFORE TRUNCATE ON trainset_sources
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

-- A dataset's kind never changes once it has versions: a pool cannot become a trainset or the other way round.
CREATE FUNCTION guard_dataset_kind() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.kind IS DISTINCT FROM OLD.kind AND EXISTS (SELECT 1 FROM versions WHERE dataset_id = OLD.id) THEN
    RAISE EXCEPTION 'dataset % has versions: its kind cannot change', OLD.name;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER datasets_kind_guard BEFORE UPDATE ON datasets
  FOR EACH ROW EXECUTE FUNCTION guard_dataset_kind();

-- Every version of a trainset has its record, and only versions of a trainset have one. Checked when the
-- transaction commits, after the version has been frozen.
CREATE FUNCTION require_trainset_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  is_trainset boolean;
BEGIN
  SELECT d.kind = 'trainset' INTO is_trainset FROM versions v JOIN datasets d ON d.id = v.dataset_id
  WHERE v.id = NEW.id;
  IF is_trainset IS DISTINCT FROM EXISTS (SELECT 1 FROM trainset_versions WHERE version_id = NEW.id) THEN
    RAISE EXCEPTION 'version %: a trainset version needs its trainset record, and only those have one', NEW.id;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER versions_trainset_record AFTER INSERT ON versions
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_trainset_record();
