-- 0004: the training code of a model version is optional (2026-11-06): metrics come from the platform's own
-- evaluation, not from the training framework, so a version may record no commit. A repository or the
-- "uncommitted changes" flag still needs a commit. model.onnx, weights/<name> and files/... stay in the file set.
ALTER TABLE model_versions ALTER COLUMN code_commit DROP NOT NULL;
ALTER TABLE model_versions ADD CONSTRAINT model_versions_code_needs_commit
  CHECK (code_commit IS NOT NULL OR (code_repo = '' AND NOT code_dirty));
