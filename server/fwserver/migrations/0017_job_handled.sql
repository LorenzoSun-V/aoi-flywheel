-- 0017: a failed job marked as handled.
--
-- A failed job waits for the account that submitted it (the home page's 「待我处理」) until its work is submitted
-- again (a later job of the same kind on the same model version and data, or a check of the same runner;
-- worked out when asked, nothing stored) or someone decides it needs no new run and marks it handled: one row
-- here, with who, when and an optional note. The job row stays as it ended (0005's guard refuses any change to
-- a finished job). A mark is never changed or removed, and only a failed job can have one.

CREATE TABLE job_handled (
  job_id      bigint PRIMARY KEY REFERENCES jobs(id),
  handled_by  bigint NOT NULL REFERENCES users(id),
  handled_at  timestamptz NOT NULL DEFAULT now(),
  note        text NOT NULL DEFAULT '' CHECK (length(note) <= 2000)
);

CREATE TRIGGER job_handled_append_only BEFORE UPDATE OR DELETE ON job_handled
  FOR EACH ROW EXECUTE FUNCTION guard_append_only();
CREATE TRIGGER job_handled_truncate BEFORE TRUNCATE ON job_handled
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

CREATE FUNCTION check_job_handled() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM jobs WHERE id = NEW.job_id AND status = 'failed') THEN
    RAISE EXCEPTION 'job_handled: job % has not failed', NEW.job_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER job_handled_check BEFORE INSERT ON job_handled
  FOR EACH ROW EXECUTE FUNCTION check_job_handled();
