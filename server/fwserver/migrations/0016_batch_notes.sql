-- 0016: a note on each problem image of a rejection. A reviewer who sends a batch back points at the images
-- that need work (images, 0010); each of those may now carry a short note of what is wrong with it, so that
-- the labeler reads the problem beside the image instead of looking for it in the comment.
--
-- notes maps a path of images to its note. Only a rejection has notes, only on images it points at, and no
-- note is empty or longer than 200 characters. Rows stay as they were: the guards of 0010 still refuse any
-- change, and rows before this one have no notes.

CREATE FUNCTION batch_step_notes_fit(notes jsonb, images jsonb) RETURNS boolean
  LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_typeof(notes) = 'object'
     AND NOT EXISTS (SELECT 1 FROM jsonb_each(notes) n
                     WHERE jsonb_typeof(n.value) <> 'string'
                        OR btrim(n.value #>> '{}') = ''
                        OR length(n.value #>> '{}') > 200
                        OR NOT images ? n.key)
$$;

ALTER TABLE batch_steps ADD COLUMN notes jsonb NOT NULL DEFAULT '{}';   -- reject: image path -> note
ALTER TABLE batch_steps ADD CONSTRAINT batch_steps_notes
  CHECK (batch_step_notes_fit(notes, images) AND (step = 'reject' OR notes = '{}'));
