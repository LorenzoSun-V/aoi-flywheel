-- 0018: the reviewer of a batch in review can be changed. A batch sent to the wrong reviewer (a slip of the
-- mouse, or the reviewer is away) was stuck: only the reviewer asked could answer it. Its owner or an
-- administrator now asks another reviewer; the change is one more row of the append-only trail, naming the
-- new reviewer, like the submission that asked the first one. Rows stay as they were: the guards of 0010
-- still refuse any change.

ALTER TABLE batch_steps DROP CONSTRAINT batch_steps_step_check;
ALTER TABLE batch_steps ADD CONSTRAINT batch_steps_step_check
  CHECK (step IN ('upload', 'submit', 'reject', 'approve', 'import', 'close', 'prelabel', 'reassign'));
ALTER TABLE batch_steps DROP CONSTRAINT batch_steps_check1;   -- (step = 'submit') = (reviewer_id IS NOT NULL)
ALTER TABLE batch_steps ADD CONSTRAINT batch_steps_reviewer
  CHECK ((step IN ('submit', 'reassign')) = (reviewer_id IS NOT NULL));
