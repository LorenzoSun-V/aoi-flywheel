-- 0015: the benchmark test set of a production line.
--
-- Models of a line are compared on one test set, the line's benchmark: the overview and a model's page show
-- the numbers of each model on the newest version of that test set, evaluated as a whole, and nothing else,
-- so that two numbers side by side were measured on the same images. Before this, the overview put the last
-- report of each model side by side, whatever it was evaluated on.
--
-- A line's benchmark is a test set of that line. It may be changed to another one; every setting is a row,
-- never changed or removed, so which test set was the line's benchmark at any time stays known. The line's
-- benchmark now is its newest row. A line that never had one has no rows.

CREATE TABLE line_benchmarks (
  id          bigserial PRIMARY KEY,
  line_id     bigint NOT NULL REFERENCES lines(id),
  dataset_id  bigint NOT NULL REFERENCES datasets(id),
  note        text NOT NULL DEFAULT '' CHECK (length(note) <= 2000),
  created_by  bigint NOT NULL REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX line_benchmarks_line ON line_benchmarks (line_id, id);

-- Only ever added: changing the benchmark is a new row.
CREATE TRIGGER line_benchmarks_append_only BEFORE UPDATE OR DELETE ON line_benchmarks
  FOR EACH ROW EXECUTE FUNCTION guard_append_only();
CREATE TRIGGER line_benchmarks_truncate BEFORE TRUNCATE ON line_benchmarks
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

-- The test set is one of the line's own (kind 'test', on the same site and line, in any case of letters).
CREATE FUNCTION check_line_benchmark() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM datasets d JOIN lines l ON l.id = NEW.line_id JOIN sites s ON s.id = l.site_id
                 WHERE d.id = NEW.dataset_id AND d.kind = 'test'
                   AND lower(d.site) = lower(s.code) AND lower(d.line) = lower(l.code)) THEN
    RAISE EXCEPTION 'line_benchmarks: dataset % is not a test set of line %', NEW.dataset_id, NEW.line_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER line_benchmarks_check BEFORE INSERT ON line_benchmarks
  FOR EACH ROW EXECUTE FUNCTION check_line_benchmark();
