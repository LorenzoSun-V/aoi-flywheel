-- 0007: prediction sets (step 3, part 3). A worker runs a model version's TensorRT engine over chosen folders
-- of a dataset version and sends back what it found; the platform keeps that as a prediction set, which is
-- never changed. Pre-labels are written from a prediction set at a score threshold (fw predict get).

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('check', 'eval', 'predict'));

-- Files: predictions.json (every image of the input list: its size and its boxes at or above conf) and
-- engine.json (what the engine was built from). inputs_sha256: the manifest of the images the job read (in
-- the store). One prediction set per job, and per (model version, runner version, input list, conf): the same
-- request again reuses it instead of running the GPU again.
CREATE TABLE prediction_sets (
  id                  bigserial PRIMARY KEY,
  model_version_id    bigint NOT NULL REFERENCES model_versions(id),
  runner_version_id   bigint NOT NULL REFERENCES runner_versions(id),
  dataset_version_id  bigint NOT NULL REFERENCES versions(id),
  dirs                text[] NOT NULL DEFAULT '{}',
  inputs_sha256       bytea NOT NULL CHECK (length(inputs_sha256) = 32),
  conf                double precision NOT NULL CHECK (conf > 0 AND conf < 1),
  images              int NOT NULL CHECK (images > 0),
  boxes               bigint NOT NULL CHECK (boxes >= 0),
  fileset_id          bigint NOT NULL UNIQUE REFERENCES filesets(id),
  job_id              bigint NOT NULL UNIQUE REFERENCES jobs(id),
  created_by          bigint NOT NULL REFERENCES users(id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (model_version_id, runner_version_id, inputs_sha256, conf)
);
CREATE INDEX prediction_sets_dataset_version ON prediction_sets (dataset_version_id);

CREATE FUNCTION check_prediction_set() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = NEW.job_id AND j.kind = 'predict' AND j.status = 'running'
                   AND j.runner_version_id = NEW.runner_version_id) THEN
    RAISE EXCEPTION 'prediction set: job % is not a running prediction in that runner', NEW.job_id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM filesets s JOIN fileset_files f ON f.fileset_id = s.id
                 WHERE s.id = NEW.fileset_id AND s.frozen AND f.path = 'predictions.json') THEN
    RAISE EXCEPTION 'prediction set: its file set must be frozen and hold predictions.json';
  END IF;
  IF EXISTS (SELECT 1 FROM unnest(NEW.dirs) d WHERE d = '' OR d LIKE '/%' OR d LIKE '%/') THEN
    RAISE EXCEPTION 'prediction set: folders are relative paths without a leading or trailing /';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER prediction_sets_check BEFORE INSERT ON prediction_sets
  FOR EACH ROW EXECUTE FUNCTION check_prediction_set();
CREATE TRIGGER prediction_sets_guard BEFORE UPDATE OR DELETE ON prediction_sets
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER prediction_sets_truncate BEFORE TRUNCATE ON prediction_sets
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

-- A file set now belongs to exactly one model version, evaluation report or prediction set.
CREATE OR REPLACE FUNCTION require_fileset_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM filesets WHERE id = NEW.id AND NOT frozen) THEN
    RAISE EXCEPTION 'file set % must be frozen before its transaction commits', NEW.id;
  END IF;
  IF (SELECT count(*) FROM model_versions WHERE fileset_id = NEW.id)
     + (SELECT count(*) FROM eval_reports WHERE fileset_id = NEW.id)
     + (SELECT count(*) FROM prediction_sets WHERE fileset_id = NEW.id) <> 1 THEN
    RAISE EXCEPTION 'file set % must belong to exactly one model version, evaluation report or prediction set',
      NEW.id;
  END IF;
  RETURN NULL;
END $$;
