-- 0021: registering a runner from the web. A runner records what one GPU machine has (its package listing,
-- source commit, onnx2engine program), so only the worker on that machine can read it: an administrator or an
-- algorithm engineer asks for it on the web, as a job of kind 'register' for that machine, and the worker there
-- registers the runner as `fw runner register` does. The job names its machine in its params (frozen like the
-- rest of a job, 0005), and only that worker account claims it.

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('check', 'eval', 'predict', 'register'));
ALTER TABLE jobs ADD CONSTRAINT jobs_register_machine CHECK (
  kind <> 'register' OR (runner_version_id IS NULL AND coalesce(jsonb_typeof(params->'machine'->'id'), '') = 'number'));
