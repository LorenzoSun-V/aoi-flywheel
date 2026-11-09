-- 0005: the job queue for GPU workers (step 3), worker accounts and runners (inference environments).
--
-- A runner is a named inference environment: a conda environment on a worker machine, plus, optionally, the
-- framework source it runs. Each registration that differs from the last one adds a runner version (which
-- machine, which environment listing, which source commit), never changed or removed, so a job or a report
-- names exactly what it ran in. Moving to another GPU machine = registering the same name there again.
--
-- A job is queued, then claimed by a worker under a lease: each claim makes a new random token (only its
-- sha256 is kept here). Renewing, logging and finishing must present it before the lease expires; a claim
-- that lost its lease (expired, cancelled, given to another worker) can no longer change the job.

ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'algo', 'field', 'deploy', 'worker'));

-- --- runners ------------------------------------------------------------------------------------------
CREATE TABLE runners (
  id          bigserial PRIMARY KEY,
  name        text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX runners_name ON runners (lower(name));

CREATE TABLE runner_versions (
  id             bigserial PRIMARY KEY,
  runner_id      bigint NOT NULL REFERENCES runners(id),
  number         int NOT NULL CHECK (number > 0),
  worker_id      bigint NOT NULL REFERENCES users(id),  -- the worker account of the machine it is on
  conda_env      text NOT NULL CHECK (conda_env <> ''),
  -- sha256 of `conda list -n <env> --explicit`; the listing itself is in the store under that hash
  env_sha256     bytea NOT NULL CHECK (length(env_sha256) = 32),
  src_dir        text NOT NULL DEFAULT '',
  src_commit     text CHECK (src_commit ~ '^[0-9a-f]{7,64}$'),
  src_dirty      boolean NOT NULL DEFAULT false,
  registered_by  bigint NOT NULL REFERENCES users(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (runner_id, number),
  CHECK (src_dir <> '' OR (src_commit IS NULL AND NOT src_dirty))
);

CREATE FUNCTION guard_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% rows are never changed or removed', TG_TABLE_NAME;
END $$;
CREATE TRIGGER runner_versions_append_only BEFORE UPDATE OR DELETE ON runner_versions
  FOR EACH ROW EXECUTE FUNCTION guard_append_only();
CREATE TRIGGER runners_append_only BEFORE UPDATE OR DELETE ON runners
  FOR EACH ROW EXECUTE FUNCTION guard_append_only();

-- --- jobs ---------------------------------------------------------------------------------------------
CREATE TABLE jobs (
  id                bigserial PRIMARY KEY,
  kind              text NOT NULL CHECK (kind IN ('check')),
  params            jsonb NOT NULL,
  runner_version_id bigint REFERENCES runner_versions(id),  -- runs only on the worker that has this runner
  status            text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  submitted_by      bigint NOT NULL REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  attempts          int NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts      int NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  worker_id         bigint REFERENCES users(id),
  lease_sha256      bytea CHECK (length(lease_sha256) = 32),
  lease_expires     timestamptz,
  cancel_requested  boolean NOT NULL DEFAULT false,
  started_at        timestamptz,
  finished_at       timestamptz,
  result            jsonb,
  error             text,
  -- a running job always has its lease; nothing else has one
  CHECK ((status = 'running') = (lease_sha256 IS NOT NULL AND lease_expires IS NOT NULL AND worker_id IS NOT NULL)),
  CHECK ((status IN ('done', 'failed', 'cancelled')) = (finished_at IS NOT NULL)),
  CHECK (status <> 'done' OR result IS NOT NULL),
  CHECK (attempts <= max_attempts)
);
CREATE INDEX jobs_queued ON jobs (id) WHERE status = 'queued';
CREATE INDEX jobs_running ON jobs (lease_expires) WHERE status = 'running';

-- A finished job is final.
CREATE FUNCTION guard_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'jobs are never removed';
  END IF;
  IF OLD.status IN ('done', 'failed', 'cancelled') THEN
    RAISE EXCEPTION 'job % is finished (%): it cannot change', OLD.id, OLD.status;
  END IF;
  IF NEW.id <> OLD.id OR NEW.kind <> OLD.kind OR NEW.params <> OLD.params
     OR NEW.runner_version_id IS DISTINCT FROM OLD.runner_version_id OR NEW.submitted_by <> OLD.submitted_by
     OR NEW.created_at <> OLD.created_at OR NEW.attempts < OLD.attempts THEN
    RAISE EXCEPTION 'what a job is cannot change';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER jobs_guard BEFORE UPDATE OR DELETE ON jobs FOR EACH ROW EXECUTE FUNCTION guard_job();

-- One row per claim: who ran it, how it ended (done, failed, cancelled, lost = the lease expired).
CREATE TABLE job_attempts (
  job_id      bigint NOT NULL REFERENCES jobs(id),
  attempt     int NOT NULL CHECK (attempt > 0),
  worker_id   bigint NOT NULL REFERENCES users(id),
  started_at  timestamptz NOT NULL DEFAULT now(),
  ended_at    timestamptz,
  outcome     text CHECK (outcome IN ('done', 'failed', 'cancelled', 'lost')),
  lease_sha256 bytea NOT NULL CHECK (length(lease_sha256) = 32),  -- lets a retried finish find its claim
  PRIMARY KEY (job_id, attempt),
  CHECK ((ended_at IS NULL) = (outcome IS NULL))
);

-- The output a worker sends back while a job runs, in pieces, per attempt.
CREATE TABLE job_logs (
  job_id   bigint NOT NULL,
  attempt  int NOT NULL,
  seq      int NOT NULL CHECK (seq >= 0),
  at       timestamptz NOT NULL DEFAULT now(),
  text     text NOT NULL,
  PRIMARY KEY (job_id, attempt, seq),
  FOREIGN KEY (job_id, attempt) REFERENCES job_attempts (job_id, attempt)
);
