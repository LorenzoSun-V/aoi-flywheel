-- 0001: accounts, datasets and their frozen versions.
-- Applied files are never edited: every change to the schema is a new numbered file.

CREATE TABLE users (
  id           bigserial PRIMARY KEY,
  name         text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  role         text NOT NULL CHECK (role IN ('admin', 'algo', 'field', 'deploy')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  disabled_at  timestamptz
);
CREATE UNIQUE INDEX users_name ON users (lower(name));

-- Only the sha256 of a token is stored; the token itself is shown once when it is created.
CREATE TABLE tokens (
  id            bigserial PRIMARY KEY,
  user_id       bigint NOT NULL REFERENCES users(id),
  token_sha256  bytea NOT NULL UNIQUE CHECK (length(token_sha256) = 32),
  label         text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz,
  revoked_at    timestamptz
);

CREATE TABLE datasets (
  id          bigserial PRIMARY KEY,
  name        text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  site        text NOT NULL DEFAULT '',
  line        text NOT NULL DEFAULT '',
  kind        text NOT NULL DEFAULT 'pool' CHECK (kind IN ('pool', 'test', 'regression', 'other')),
  note        text NOT NULL DEFAULT '',
  created_by  bigint NOT NULL REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX datasets_name ON datasets (lower(name));

-- A version is inserted unfrozen, gets all its files in the same transaction, and is frozen before that
-- transaction commits. Once frozen it can never change (see the triggers below).
CREATE TABLE versions (
  id               bigserial PRIMARY KEY,
  dataset_id       bigint NOT NULL REFERENCES datasets(id),
  number           int NOT NULL CHECK (number > 0),
  parent_id        bigint REFERENCES versions(id),
  frozen           boolean NOT NULL DEFAULT false,
  manifest_sha256  bytea NOT NULL CHECK (length(manifest_sha256) = 32),
  file_count       bigint NOT NULL CHECK (file_count >= 0),
  total_bytes      bigint NOT NULL CHECK (total_bytes >= 0),
  source           text NOT NULL DEFAULT '',
  note             text NOT NULL DEFAULT '',
  created_by       bigint NOT NULL REFERENCES users(id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (dataset_id, number)
);

-- The full file list of every version: reading a version never goes through its parent.
CREATE TABLE version_files (
  version_id  bigint NOT NULL REFERENCES versions(id),
  path        text COLLATE "C" NOT NULL,
  sha256      bytea NOT NULL CHECK (length(sha256) = 32),
  size        bigint NOT NULL CHECK (size >= 0),
  PRIMARY KEY (version_id, path)
);

-- sha256 of the canonical manifest (docs/manifest-format.md) rebuilt from a version's rows.
CREATE FUNCTION version_manifest_sha256(vid bigint) RETURNS bytea LANGUAGE sql STABLE AS $$
  SELECT sha256(convert_to(
           'fw-manifest 1' || E'\n' ||
           coalesce(string_agg(path || E'\t' || encode(sha256, 'hex') || E'\t' || size::text || E'\n', ''
                               ORDER BY path), ''),
           'UTF8'))
  FROM version_files WHERE version_id = vid
$$;

-- versions: inserted unfrozen; frozen only if the rows rebuild exactly the recorded manifest hash, file
-- count and size; after that no UPDATE (so no unfreezing) and no DELETE.
CREATE FUNCTION guard_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
  total bigint;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.frozen THEN
      RAISE EXCEPTION 'version must be inserted unfrozen and frozen after its files are written';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.frozen THEN
    RAISE EXCEPTION 'version % is frozen: it cannot be changed, unfrozen or deleted', OLD.id;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  IF NEW.id <> OLD.id THEN
    RAISE EXCEPTION 'version id cannot change';
  END IF;
  IF NEW.frozen THEN
    SELECT count(*), coalesce(sum(size), 0) INTO n, total FROM version_files WHERE version_id = NEW.id;
    IF NEW.manifest_sha256 IS DISTINCT FROM version_manifest_sha256(NEW.id)
       OR NEW.file_count <> n OR NEW.total_bytes <> total THEN
      RAISE EXCEPTION 'version %: its files do not match the recorded manifest, not freezing', NEW.id;
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER versions_guard BEFORE INSERT OR UPDATE OR DELETE ON versions
  FOR EACH ROW EXECUTE FUNCTION guard_version();

-- No version may be committed unfrozen.
CREATE FUNCTION require_frozen_at_commit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM versions WHERE id = NEW.id AND NOT frozen) THEN
    RAISE EXCEPTION 'version % must be frozen before its transaction commits', NEW.id;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER versions_frozen_at_commit AFTER INSERT ON versions
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_frozen_at_commit();

-- version_files: rows of a frozen version cannot be added, changed or removed. One function per kind of
-- statement: a transition table only exists for the statements that define it.
CREATE FUNCTION refuse_frozen_new_rows() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM new_rows n JOIN versions v ON v.id = n.version_id WHERE v.frozen) THEN
    RAISE EXCEPTION 'files cannot be added to or changed in a frozen version';
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION refuse_frozen_old_rows() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM old_rows o JOIN versions v ON v.id = o.version_id WHERE v.frozen) THEN
    RAISE EXCEPTION 'files of a frozen version cannot be changed or removed';
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER version_files_insert AFTER INSERT ON version_files
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION refuse_frozen_new_rows();
CREATE TRIGGER version_files_update_new AFTER UPDATE ON version_files
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION refuse_frozen_new_rows();
CREATE TRIGGER version_files_update_old AFTER UPDATE ON version_files
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION refuse_frozen_old_rows();
CREATE TRIGGER version_files_delete AFTER DELETE ON version_files
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION refuse_frozen_old_rows();

CREATE FUNCTION refuse_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% cannot be truncated', TG_TABLE_NAME;
END $$;
CREATE TRIGGER versions_truncate BEFORE TRUNCATE ON versions
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER version_files_truncate BEFORE TRUNCATE ON version_files
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
