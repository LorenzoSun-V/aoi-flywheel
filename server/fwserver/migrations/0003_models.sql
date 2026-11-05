-- 0003: models, model versions, evaluation reports, releases and model tags.
-- Every record is written once and never changed or removed; tags are an append-only log of additions and
-- removals. The files a model version or an evaluation report holds form a file set: a frozen list of
-- (path, sha256, size) built and checked exactly like a dataset version's, with its manifest in the store.
-- Engines are built from the ONNX by the GPU worker (step 3); nothing here stores an uploaded engine.

-- --- file sets ----------------------------------------------------------------------------------------
CREATE TABLE filesets (
  id               bigserial PRIMARY KEY,
  frozen           boolean NOT NULL DEFAULT false,
  manifest_sha256  bytea NOT NULL CHECK (length(manifest_sha256) = 32),
  file_count       bigint NOT NULL CHECK (file_count > 0),
  total_bytes      bigint NOT NULL CHECK (total_bytes >= 0),
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX filesets_manifest ON filesets (manifest_sha256);

CREATE TABLE fileset_files (
  fileset_id  bigint NOT NULL REFERENCES filesets(id),
  path        text COLLATE "C" NOT NULL,
  sha256      bytea NOT NULL CHECK (length(sha256) = 32),
  size        bigint NOT NULL CHECK (size >= 0),
  PRIMARY KEY (fileset_id, path)
);
CREATE INDEX fileset_files_sha256 ON fileset_files (sha256);

-- The same canonical manifest as version_manifest_sha256 (docs/manifest-format.md).
CREATE FUNCTION fileset_manifest_sha256(fid bigint) RETURNS bytea LANGUAGE sql STABLE AS $$
  SELECT sha256(convert_to(
           'fw-manifest 1' || E'\n' ||
           coalesce(string_agg(path || E'\t' || encode(sha256, 'hex') || E'\t' || size::text || E'\n', ''
                               ORDER BY path), ''),
           'UTF8'))
  FROM fileset_files WHERE fileset_id = fid
$$;

-- Inserted unfrozen, frozen once its rows rebuild the recorded manifest, then never changed or removed.
CREATE FUNCTION guard_fileset() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
  total bigint;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.frozen THEN
      RAISE EXCEPTION 'file set must be inserted unfrozen and frozen after its files are written';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.frozen THEN
    RAISE EXCEPTION 'file set % is frozen: it cannot be changed, unfrozen or deleted', OLD.id;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  IF NEW.id <> OLD.id THEN
    RAISE EXCEPTION 'file set id cannot change';
  END IF;
  IF NEW.frozen THEN
    SELECT count(*), coalesce(sum(size), 0) INTO n, total FROM fileset_files WHERE fileset_id = NEW.id;
    IF NEW.manifest_sha256 IS DISTINCT FROM fileset_manifest_sha256(NEW.id)
       OR NEW.file_count <> n OR NEW.total_bytes <> total THEN
      RAISE EXCEPTION 'file set %: its files do not match the recorded manifest, not freezing', NEW.id;
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER filesets_guard BEFORE INSERT OR UPDATE OR DELETE ON filesets
  FOR EACH ROW EXECUTE FUNCTION guard_fileset();

-- Committed only frozen, and held by exactly one model version or evaluation report.
CREATE FUNCTION require_fileset_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM filesets WHERE id = NEW.id AND NOT frozen) THEN
    RAISE EXCEPTION 'file set % must be frozen before its transaction commits', NEW.id;
  END IF;
  IF (SELECT count(*) FROM model_versions WHERE fileset_id = NEW.id)
     + (SELECT count(*) FROM eval_reports WHERE fileset_id = NEW.id) <> 1 THEN
    RAISE EXCEPTION 'file set % must belong to exactly one model version or evaluation report', NEW.id;
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION refuse_frozen_fileset_new_rows() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM new_rows n JOIN filesets s ON s.id = n.fileset_id WHERE s.frozen) THEN
    RAISE EXCEPTION 'files cannot be added to or changed in a frozen file set';
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION refuse_frozen_fileset_old_rows() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM old_rows o JOIN filesets s ON s.id = o.fileset_id WHERE s.frozen) THEN
    RAISE EXCEPTION 'files of a frozen file set cannot be changed or removed';
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER fileset_files_insert AFTER INSERT ON fileset_files
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION refuse_frozen_fileset_new_rows();
CREATE TRIGGER fileset_files_update_new AFTER UPDATE ON fileset_files
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION refuse_frozen_fileset_new_rows();
CREATE TRIGGER fileset_files_update_old AFTER UPDATE ON fileset_files
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION refuse_frozen_fileset_old_rows();
CREATE TRIGGER fileset_files_delete AFTER DELETE ON fileset_files
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION refuse_frozen_fileset_old_rows();

-- --- models and their versions ------------------------------------------------------------------------
CREATE TABLE models (
  id          bigserial PRIMARY KEY,
  name        text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  site        text NOT NULL CHECK (site ~ '^[A-Za-z0-9_.\u3400-\u4DBF\u4E00-\u9FFF]+$'),  -- = models.SITE_LINE: release refs are <site>-<line>-r<n>
  line        text NOT NULL CHECK (line ~ '^[A-Za-z0-9_.\u3400-\u4DBF\u4E00-\u9FFF]+$'),
  task        text NOT NULL CHECK (task IN ('det')),
  note        text NOT NULL DEFAULT '',
  created_by  bigint NOT NULL REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX models_name ON models (lower(name));

-- Files: model.onnx, classes.txt (the snapshot's), metrics.json (the validation metrics, if given) and
-- files/... (weights, configs). metrics is metrics.json parsed, {} without one.
CREATE TABLE model_versions (
  id           bigserial PRIMARY KEY,
  model_id     bigint NOT NULL REFERENCES models(id),
  number       int NOT NULL CHECK (number > 0),
  fileset_id   bigint NOT NULL UNIQUE REFERENCES filesets(id),
  onnx_sha256  bytea NOT NULL CHECK (length(onnx_sha256) = 32),
  trainset_id  bigint NOT NULL REFERENCES versions(id),
  code_repo    text NOT NULL DEFAULT '' CHECK (code_repo !~* '^https?://[^/]*@'),  -- never with a token in it
  code_commit  text NOT NULL CHECK (code_commit ~ '^[0-9a-f]{7,64}$'),
  code_dirty   boolean NOT NULL DEFAULT false,  -- the training code had uncommitted changes
  metrics      jsonb NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
  note         text NOT NULL DEFAULT '',
  created_by   bigint NOT NULL REFERENCES users(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (model_id, number)
);
CREATE INDEX model_versions_onnx ON model_versions (model_id, onnx_sha256);

CREATE FUNCTION check_model_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM versions v JOIN datasets d ON d.id = v.dataset_id
                 WHERE v.id = NEW.trainset_id AND d.kind = 'trainset') THEN
    RAISE EXCEPTION 'model version: % is not a training-set snapshot', NEW.trainset_id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM filesets s JOIN fileset_files f ON f.fileset_id = s.id
                 WHERE s.id = NEW.fileset_id AND s.frozen AND f.path = 'model.onnx' AND f.sha256 = NEW.onnx_sha256) THEN
    RAISE EXCEPTION 'model version: its file set must be frozen and hold model.onnx with the recorded hash';
  END IF;
  IF EXISTS (SELECT 1 FROM eval_reports WHERE fileset_id = NEW.fileset_id) THEN
    RAISE EXCEPTION 'model version: file set % belongs to an evaluation report', NEW.fileset_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER model_versions_check BEFORE INSERT ON model_versions
  FOR EACH ROW EXECUTE FUNCTION check_model_version();

-- Tags mark candidates and deprecated versions. A change is a new row; the newest row per tag says whether it
-- is on.
CREATE TABLE model_tag_events (
  id                bigserial PRIMARY KEY,
  model_version_id  bigint NOT NULL REFERENCES model_versions(id),
  tag               text NOT NULL CHECK (tag IN ('candidate', 'deprecated')),
  added             boolean NOT NULL,
  note              text NOT NULL DEFAULT '',
  created_by        bigint NOT NULL REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_tag_events_version ON model_tag_events (model_version_id, tag, id);

-- --- evaluation reports -------------------------------------------------------------------------------
-- Files: metrics.json and files/... (for aoi-deteval: its whole output folder). split names the part of a
-- training-set snapshot that was evaluated; it is empty for any other test set.
CREATE TABLE eval_reports (
  id                bigserial PRIMARY KEY,
  model_version_id  bigint NOT NULL REFERENCES model_versions(id),
  testset_id        bigint NOT NULL REFERENCES versions(id),
  split             text NOT NULL DEFAULT '' CHECK (split IN ('', 'train', 'val', 'test')),
  fileset_id        bigint NOT NULL UNIQUE REFERENCES filesets(id),
  metrics           jsonb NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
  tool              text NOT NULL DEFAULT '',
  tool_version      text NOT NULL DEFAULT '',
  note              text NOT NULL DEFAULT '',
  created_by        bigint NOT NULL REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_reports_version ON eval_reports (model_version_id);

CREATE FUNCTION check_eval_report() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  kind text;
BEGIN
  SELECT d.kind INTO kind FROM versions v JOIN datasets d ON d.id = v.dataset_id WHERE v.id = NEW.testset_id;
  IF (NEW.split <> '') <> (kind = 'trainset') THEN
    RAISE EXCEPTION 'evaluation report: a split is named for a training-set snapshot, and only for one';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM filesets s JOIN fileset_files f ON f.fileset_id = s.id
                 WHERE s.id = NEW.fileset_id AND s.frozen AND f.path = 'metrics.json') THEN
    RAISE EXCEPTION 'evaluation report: its file set must be frozen and hold metrics.json';
  END IF;
  IF EXISTS (SELECT 1 FROM model_versions WHERE fileset_id = NEW.fileset_id) THEN
    RAISE EXCEPTION 'evaluation report: file set % belongs to a model version', NEW.fileset_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER eval_reports_check BEFORE INSERT ON eval_reports
  FOR EACH ROW EXECUTE FUNCTION check_eval_report();

-- --- releases -----------------------------------------------------------------------------------------
-- What a production line runs: the model version, the deploy repository and commit, and how the site built
-- its engine from the ONNX (GPU, TensorRT version, precision, the engine's hash; the engine file itself stays
-- on the site). The line's current model is the release with the latest deployed_at, so a release registered
-- late with an earlier deployed_at never displaces the one actually deployed after it.
CREATE TABLE releases (
  id                bigserial PRIMARY KEY,
  site              text NOT NULL CHECK (site ~ '^[A-Za-z0-9_.\u3400-\u4DBF\u4E00-\u9FFF]+$'),  -- = models.SITE_LINE: a ref names one line
  line              text NOT NULL CHECK (line ~ '^[A-Za-z0-9_.\u3400-\u4DBF\u4E00-\u9FFF]+$'),
  number            int NOT NULL CHECK (number > 0),
  model_version_id  bigint NOT NULL REFERENCES model_versions(id),
  deploy_repo       text NOT NULL CHECK (deploy_repo <> '' AND deploy_repo !~* '^https?://[^/]*@'),
  deploy_commit     text NOT NULL CHECK (deploy_commit ~ '^[0-9a-f]{7,64}$'),
  deployed_at       timestamptz NOT NULL,
  gpu               text NOT NULL DEFAULT '',
  trt_version       text NOT NULL DEFAULT '',
  engine_precision  text NOT NULL DEFAULT '' CHECK (engine_precision IN ('', 'fp32', 'fp16')),
  engine_sha256     bytea CHECK (engine_sha256 IS NULL OR length(engine_sha256) = 32),
  engine_size       bigint CHECK (engine_size IS NULL OR engine_size > 0),
  note              text NOT NULL DEFAULT '',
  created_by        bigint NOT NULL REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK ((engine_sha256 IS NULL) = (engine_size IS NULL))
);
CREATE UNIQUE INDEX releases_number ON releases (lower(site), lower(line), number);
CREATE INDEX releases_current ON releases (lower(site), lower(line), deployed_at DESC, id DESC);
CREATE INDEX releases_model_version ON releases (model_version_id);

-- --- nothing above is ever changed or removed ---------------------------------------------------------
CREATE FUNCTION refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% rows cannot be changed or removed', TG_TABLE_NAME;
END $$;
CREATE TRIGGER models_guard BEFORE UPDATE OR DELETE ON models
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER model_versions_guard BEFORE UPDATE OR DELETE ON model_versions
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER model_tag_events_guard BEFORE UPDATE OR DELETE ON model_tag_events
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER eval_reports_guard BEFORE UPDATE OR DELETE ON eval_reports
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER releases_guard BEFORE UPDATE OR DELETE ON releases
  FOR EACH ROW EXECUTE FUNCTION refuse_change();

CREATE CONSTRAINT TRIGGER filesets_owner_at_commit AFTER INSERT ON filesets
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_fileset_owner();

CREATE TRIGGER filesets_truncate BEFORE TRUNCATE ON filesets
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER fileset_files_truncate BEFORE TRUNCATE ON fileset_files
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER models_truncate BEFORE TRUNCATE ON models
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER model_versions_truncate BEFORE TRUNCATE ON model_versions
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER model_tag_events_truncate BEFORE TRUNCATE ON model_tag_events
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER eval_reports_truncate BEFORE TRUNCATE ON eval_reports
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER releases_truncate BEFORE TRUNCATE ON releases
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
