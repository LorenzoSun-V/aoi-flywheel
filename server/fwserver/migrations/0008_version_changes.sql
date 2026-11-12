-- 0008: how a version was made from its parent by the server (fw dataset add / remove, the web's "上传新增目录"
-- and "去掉目录"): the folders that were added, replaced or removed. The server builds such a version itself
-- from the parent's rows and the uploaded folder's own file list, so these rows say what happened, not what
-- a client claims. Written in the transaction that creates the version, before it is frozen; never changed or
-- removed afterwards. A version imported whole (fw dataset import) has no rows here.

CREATE TABLE version_changes (
  version_id      bigint NOT NULL REFERENCES versions(id),
  position        int NOT NULL CHECK (position >= 0),
  op              text NOT NULL CHECK (op IN ('add', 'replace', 'remove')),
  dir             text COLLATE "C" NOT NULL CHECK (dir <> '' AND dir NOT LIKE '/%' AND dir NOT LIKE '%/'),
  -- files the folder holds in the new version (add, replace; 0 for remove) and their size
  file_count      bigint NOT NULL CHECK (file_count >= 0),
  total_bytes     bigint NOT NULL CHECK (total_bytes >= 0),
  -- files of the parent's folder that are gone (remove, replace; 0 for add)
  removed_count   bigint NOT NULL CHECK (removed_count >= 0),
  -- the uploaded file list of the folder (a manifest with paths inside the folder), in the store
  content_sha256  bytea CHECK (length(content_sha256) = 32),
  PRIMARY KEY (version_id, position),
  CHECK ((op = 'remove') = (content_sha256 IS NULL)),
  CHECK (op <> 'remove' OR file_count = 0),
  CHECK (op <> 'add' OR removed_count = 0),
  CHECK (op = 'add' OR removed_count > 0),
  CHECK (op = 'remove' OR file_count > 0)
);

-- Rows may be added only for a version that is not frozen yet and has a parent; nothing is ever changed or
-- removed.
CREATE FUNCTION guard_version_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION 'version_changes rows cannot be changed or removed';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM versions v JOIN datasets d ON d.id = v.dataset_id
                 WHERE v.id = NEW.version_id AND NOT v.frozen AND d.kind <> 'trainset'
                   AND (v.parent_id IS NOT NULL OR NEW.op = 'add')) THEN
    RAISE EXCEPTION 'version %: frozen, a snapshot, or without a parent to change: no version_changes row',
      NEW.version_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER version_changes_guard BEFORE INSERT OR UPDATE OR DELETE ON version_changes
  FOR EACH ROW EXECUTE FUNCTION guard_version_change();
CREATE TRIGGER version_changes_truncate BEFORE TRUNCATE ON version_changes
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
