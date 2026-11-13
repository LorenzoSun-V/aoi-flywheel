-- 0013: the register of sites and their production lines.
--
-- Until now a site and a line were whatever was typed when a data pool or a model was made, so "SES" and
-- "ses" were two sites in a list, and nothing could be grouped by them. From here on a pool, a model and a
-- release take only a registered site and line.
--
-- A site's or line's code is what is stored and what goes into things that never change: the folders of a
-- training snapshot (<site>/<line>/...), the keys of its samples, release numbers (SES-TX-r3). So a code is
-- fixed for good; what people may change is the name shown next to it. Nothing stored before is rewritten:
-- the rows of datasets, models and releases keep the text they have, and are found by it without regard to
-- case (SES and ses are one site). Text that cannot be a code (datasets were never checked) stays as it is
-- and is listed as not registered.

CREATE TABLE sites (
  id          bigserial PRIMARY KEY,
  code        text NOT NULL CHECK (code ~ '^[A-Za-z0-9_.㐀-䶿一-鿿]{1,64}$'),  -- = models.SITE_LINE
  name        text NOT NULL DEFAULT '' CHECK (length(name) <= 64),   -- shown next to the code; may change
  created_by  bigint REFERENCES users(id),                            -- NULL: taken over from what existed (0013)
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX sites_code ON sites (lower(code));

CREATE TABLE lines (
  id          bigserial PRIMARY KEY,
  site_id     bigint NOT NULL REFERENCES sites(id),
  code        text NOT NULL CHECK (code ~ '^[A-Za-z0-9_.㐀-䶿一-鿿]{1,64}$'),
  name        text NOT NULL DEFAULT '' CHECK (length(name) <= 64),
  created_by  bigint REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX lines_code ON lines (site_id, lower(code));

-- Only the shown name of a site or line ever changes; rows are never removed.
CREATE FUNCTION guard_place() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '% rows cannot be removed', TG_TABLE_NAME;
  END IF;
  IF (to_jsonb(NEW) - 'name') <> (to_jsonb(OLD) - 'name') THEN
    RAISE EXCEPTION '%: only the shown name can change, the code is fixed', TG_TABLE_NAME;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER sites_guard BEFORE UPDATE OR DELETE ON sites FOR EACH ROW EXECUTE FUNCTION guard_place();
CREATE TRIGGER sites_truncate BEFORE TRUNCATE ON sites FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER lines_guard BEFORE UPDATE OR DELETE ON lines FOR EACH ROW EXECUTE FUNCTION guard_place();
CREATE TRIGGER lines_truncate BEFORE TRUNCATE ON lines FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

-- What exists becomes the register: every site and line a pool, a model or a release names. Of spellings
-- that differ only in case, the one used first is the code.
WITH used AS (
  SELECT site, line, created_at FROM datasets WHERE kind NOT IN ('trainset', 'batch')
  UNION ALL SELECT site, line, created_at FROM models
  UNION ALL SELECT site, line, created_at FROM releases
)
INSERT INTO sites (code, created_at)
SELECT DISTINCT ON (lower(site)) site, created_at FROM used
WHERE site ~ '^[A-Za-z0-9_.㐀-䶿一-鿿]{1,64}$'
ORDER BY lower(site), created_at, site;

WITH used AS (
  SELECT site, line, created_at FROM datasets WHERE kind NOT IN ('trainset', 'batch')
  UNION ALL SELECT site, line, created_at FROM models
  UNION ALL SELECT site, line, created_at FROM releases
)
INSERT INTO lines (site_id, code, created_at)
SELECT DISTINCT ON (s.id, lower(u.line)) s.id, u.line, u.created_at
FROM used u JOIN sites s ON lower(s.code) = lower(u.site)
WHERE u.line ~ '^[A-Za-z0-9_.㐀-䶿一-鿿]{1,64}$'
ORDER BY s.id, lower(u.line), u.created_at, u.line;
