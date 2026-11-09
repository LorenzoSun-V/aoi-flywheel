-- 0006: evaluation jobs (step 3, part 2). A worker runs aoi-deteval in a runner's conda environment on chosen
-- folders of a dataset version (or one split of a training-set snapshot), and the platform registers the
-- result as an evaluation report that names the job, the runner version and the folders.

-- Which aoi-deteval adapter a runner's environment runs ('' = none; such a runner only runs checks).
ALTER TABLE runner_versions ADD COLUMN framework text NOT NULL DEFAULT ''
  CHECK (framework IN ('', 'ultralytics', 'rfdetr'));

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('check', 'eval'));

-- dirs: the folders of the test set that were evaluated, '{}' = all of it (always '{}' for a snapshot, whose
-- split says what was evaluated). job_id, runner_version_id: set when the platform ran the evaluation.
ALTER TABLE eval_reports ADD COLUMN dirs text[] NOT NULL DEFAULT '{}';
ALTER TABLE eval_reports ADD COLUMN job_id bigint UNIQUE REFERENCES jobs(id);
ALTER TABLE eval_reports ADD COLUMN runner_version_id bigint REFERENCES runner_versions(id);
ALTER TABLE eval_reports ADD CONSTRAINT eval_reports_job_runner
  CHECK ((job_id IS NULL) = (runner_version_id IS NULL));

CREATE FUNCTION check_eval_report_dirs() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.split <> '' AND cardinality(NEW.dirs) > 0 THEN
    RAISE EXCEPTION 'evaluation report: a split of a snapshot is evaluated whole, without folders';
  END IF;
  IF EXISTS (SELECT 1 FROM unnest(NEW.dirs) d WHERE d = '' OR d LIKE '/%' OR d LIKE '%/') THEN
    RAISE EXCEPTION 'evaluation report: folders are relative paths without a leading or trailing /';
  END IF;
  IF NEW.job_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM jobs j WHERE j.id = NEW.job_id AND j.kind = 'eval' AND j.status = 'running'
         AND j.runner_version_id = NEW.runner_version_id) THEN
    RAISE EXCEPTION 'evaluation report: job % is not a running evaluation in that runner', NEW.job_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER eval_reports_check_dirs BEFORE INSERT ON eval_reports
  FOR EACH ROW EXECUTE FUNCTION check_eval_report_dirs();
