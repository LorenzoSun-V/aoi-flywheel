-- 0012: version labels. People name versions the way they talk about them: v0.0.1 for the cold start, v0.1.0
-- when more data came in, v1.0.0 for the one that is stable. A label is another name for a version of a
-- dataset (pool, test set, training snapshot) or of a model: SES-TX@v0.1.0 is SES-TX@3.
--
-- Labels stand beside the versions; nothing frozen changes. A version may have several labels, a label
-- names one version of its dataset (or model) for good: it is never moved to another version and never
-- removed. A label put on the wrong version is retired by an administrator: it stops naming the version,
-- and the string stays taken, so that v0.1.0 written in a report can never come to mean other data.

CREATE TABLE version_labels (
  id          bigserial PRIMARY KEY,
  dataset_id  bigint NOT NULL REFERENCES datasets(id),
  version_id  bigint NOT NULL REFERENCES versions(id),
  label       text COLLATE "C" NOT NULL
              CHECK (label ~ '^v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$'),
  note        text NOT NULL DEFAULT '',
  created_by  bigint NOT NULL REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  retired_by  bigint REFERENCES users(id),
  retired_at  timestamptz,
  retired_note text NOT NULL DEFAULT '',
  UNIQUE (dataset_id, label),
  CHECK ((retired_by IS NULL) = (retired_at IS NULL)),
  CHECK (retired_at IS NOT NULL OR retired_note = '')
);
CREATE INDEX version_labels_version ON version_labels (version_id);

CREATE TABLE model_version_labels (
  id                bigserial PRIMARY KEY,
  model_id          bigint NOT NULL REFERENCES models(id),
  model_version_id  bigint NOT NULL REFERENCES model_versions(id),
  label             text COLLATE "C" NOT NULL
                    CHECK (label ~ '^v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$'),
  note              text NOT NULL DEFAULT '',
  created_by        bigint NOT NULL REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  retired_by        bigint REFERENCES users(id),
  retired_at        timestamptz,
  retired_note      text NOT NULL DEFAULT '',
  UNIQUE (model_id, label),
  CHECK ((retired_by IS NULL) = (retired_at IS NULL)),
  CHECK (retired_at IS NOT NULL OR retired_note = '')
);
CREATE INDEX model_version_labels_version ON model_version_labels (model_version_id);

-- A label row is written once; the only change ever made to it is its retirement, once.
CREATE FUNCTION guard_label() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '% rows cannot be removed', TG_TABLE_NAME;
  END IF;
  IF OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL
     OR (to_jsonb(NEW) - 'retired_by' - 'retired_at' - 'retired_note')
        <> (to_jsonb(OLD) - 'retired_by' - 'retired_at' - 'retired_note') THEN
    RAISE EXCEPTION '%: a label can only be retired, once', TG_TABLE_NAME;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER version_labels_guard BEFORE UPDATE OR DELETE ON version_labels
  FOR EACH ROW EXECUTE FUNCTION guard_label();
CREATE TRIGGER version_labels_truncate BEFORE TRUNCATE ON version_labels
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();
CREATE TRIGGER model_version_labels_guard BEFORE UPDATE OR DELETE ON model_version_labels
  FOR EACH ROW EXECUTE FUNCTION guard_label();
CREATE TRIGGER model_version_labels_truncate BEFORE TRUNCATE ON model_version_labels
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate();

-- A label names a frozen version of its own dataset; the revisions of a labelling batch take none (they
-- are counted, not named).
CREATE FUNCTION check_version_label() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.retired_at IS NOT NULL THEN
    RAISE EXCEPTION 'a label is not born retired';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM versions v JOIN datasets d ON d.id = v.dataset_id
                 WHERE v.id = NEW.version_id AND v.dataset_id = NEW.dataset_id AND v.frozen AND d.kind <> 'batch') THEN
    RAISE EXCEPTION 'label %: version % is not a frozen version of dataset %, or it is a batch revision',
      NEW.label, NEW.version_id, NEW.dataset_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER version_labels_check BEFORE INSERT ON version_labels
  FOR EACH ROW EXECUTE FUNCTION check_version_label();

CREATE FUNCTION check_model_version_label() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.retired_at IS NOT NULL THEN
    RAISE EXCEPTION 'a label is not born retired';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM model_versions v WHERE v.id = NEW.model_version_id AND v.model_id = NEW.model_id) THEN
    RAISE EXCEPTION 'label %: model version % is not a version of model %',
      NEW.label, NEW.model_version_id, NEW.model_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER model_version_labels_check BEFORE INSERT ON model_version_labels
  FOR EACH ROW EXECUTE FUNCTION check_model_version_label();
