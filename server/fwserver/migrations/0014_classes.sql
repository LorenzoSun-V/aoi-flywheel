-- 0014: the class table of a data pool.
--
-- Every label that goes into a pool, from a full upload, an added folder, a labelling batch or its import,
-- may only name classes of the pool's table: a label naming any other class is refused at upload, with the
-- files and the classes, to be corrected and uploaded again. A pool with no table takes no upload.
--
-- A class's number is its position in the table (YOLO .txt labels in the pool are numbered by it, and so are
-- snapshots continued from one another), so a table only grows: classes are added at its end, never removed,
-- renamed or moved. Each row records who added it and when, so the table a version was checked against is the
-- rows added before it.

CREATE TABLE dataset_classes (
  dataset_id  bigint NOT NULL REFERENCES datasets(id),
  position    int NOT NULL CHECK (position >= 0),
  name        text NOT NULL CHECK (name <> '' AND name = btrim(name) AND name !~ '[\t\r\n]' AND length(name) <= 128),
  created_by  bigint NOT NULL REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (dataset_id, position)
);
CREATE UNIQUE INDEX dataset_classes_name ON dataset_classes (dataset_id, lower(name));

-- Rows are only ever added, each at the end of its pool's table.
CREATE FUNCTION guard_dataset_class() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'dataset_classes: a class table only grows at its end, its rows are never changed or removed';
END $$;
CREATE TRIGGER dataset_classes_guard BEFORE UPDATE OR DELETE ON dataset_classes
  FOR EACH ROW EXECUTE FUNCTION guard_dataset_class();
CREATE TRIGGER dataset_classes_truncate BEFORE TRUNCATE ON dataset_classes
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

CREATE FUNCTION check_dataset_class() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM datasets WHERE id = NEW.dataset_id AND kind = 'pool') THEN
    RAISE EXCEPTION 'dataset_classes: dataset % is not a data pool', NEW.dataset_id;
  END IF;
  IF NEW.position <> (SELECT count(*) FROM dataset_classes WHERE dataset_id = NEW.dataset_id) THEN
    RAISE EXCEPTION 'dataset_classes: class % of dataset % is not added at the end of its table',
      NEW.name, NEW.dataset_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER dataset_classes_check BEFORE INSERT ON dataset_classes
  FOR EACH ROW EXECUTE FUNCTION check_dataset_class();
