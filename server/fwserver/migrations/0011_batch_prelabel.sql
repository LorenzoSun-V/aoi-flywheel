-- 0011: pre-labelling a batch. The owner of a batch asks for a model version to be run over its newest
-- revision (a prediction job, 0007); the batch's course keeps which job that was, so that the pre-labels the
-- owner then corrects can be traced to the model that made them.

ALTER TABLE batch_steps DROP CONSTRAINT batch_steps_step_check;
ALTER TABLE batch_steps ADD CONSTRAINT batch_steps_step_check
  CHECK (step IN ('upload', 'submit', 'reject', 'approve', 'import', 'close', 'prelabel'));
ALTER TABLE batch_steps ADD COLUMN job_id bigint REFERENCES jobs(id);   -- prelabel: the prediction job
ALTER TABLE batch_steps ADD CONSTRAINT batch_steps_prelabel_job CHECK ((step = 'prelabel') = (job_id IS NOT NULL));
-- a job is noted once per batch, however often it is asked for again
CREATE UNIQUE INDEX batch_steps_job ON batch_steps (batch_id, job_id) WHERE job_id IS NOT NULL;
