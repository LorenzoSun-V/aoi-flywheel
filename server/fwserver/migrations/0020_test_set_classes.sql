-- 0020: a test set follows the class table of a data pool. Its labels are those of the pool's line, so they
-- are checked against that pool's table at upload, the way a pool's own labels are (0014): a label naming a
-- class the table does not have is refused. The table is referred to, not copied: a pool's table only grows
-- at its end, so what fitted it once still fits.
--
-- The pool is named when the test set is made, or once later for a test set made before this: classes_from
-- goes from nothing to a pool and never changes again. It is a data pool, of the test set's line when the test
-- set has one. A test set that names none takes no upload until it does.

ALTER TABLE datasets ADD COLUMN classes_from bigint REFERENCES datasets(id);

CREATE FUNCTION guard_dataset_classes_from() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  src datasets%ROWTYPE;
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.classes_from IS NOT NULL AND NEW.classes_from IS DISTINCT FROM OLD.classes_from THEN
    RAISE EXCEPTION 'dataset %: the pool whose class table it follows cannot change', OLD.name;
  END IF;
  IF NEW.classes_from IS NOT NULL AND (TG_OP = 'INSERT' OR OLD.classes_from IS NULL) THEN
    IF NEW.kind <> 'test' THEN
      RAISE EXCEPTION 'dataset %: only a test set follows the class table of a pool', NEW.name;
    END IF;
    SELECT * INTO src FROM datasets WHERE id = NEW.classes_from;
    IF src.kind <> 'pool' THEN
      RAISE EXCEPTION 'dataset %: % is not a data pool', NEW.name, src.name;
    END IF;
    IF NEW.line <> '' AND (lower(src.site) <> lower(NEW.site) OR lower(src.line) <> lower(NEW.line)) THEN
      RAISE EXCEPTION 'dataset %: % is not a pool of its line', NEW.name, src.name;
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER datasets_classes_from_guard BEFORE INSERT OR UPDATE ON datasets
  FOR EACH ROW EXECUTE FUNCTION guard_dataset_classes_from();
