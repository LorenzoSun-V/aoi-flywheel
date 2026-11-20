-- 0022: the deploy repository and commit of a release are optional (2026-11-20). What goes to a site is the
-- ONNX of a model version; the site builds its engine on its own GPU, and not every site deploys from a git
-- repository whose commit it can give. A release records '' for each one left out (the columns stay NOT NULL,
-- so /api/v1 keeps returning strings). A repository still needs its commit, as for training code (0004), and
-- may hold no account or token.
ALTER TABLE releases DROP CONSTRAINT releases_deploy_repo_check;
ALTER TABLE releases ADD CONSTRAINT releases_deploy_repo_check CHECK (deploy_repo !~* '^https?://[^/]*@');
ALTER TABLE releases DROP CONSTRAINT releases_deploy_commit_check;
ALTER TABLE releases ADD CONSTRAINT releases_deploy_commit_check CHECK (deploy_commit ~ '^([0-9a-f]{7,64})?$');
ALTER TABLE releases ADD CONSTRAINT releases_deploy_repo_needs_commit CHECK (deploy_repo = '' OR deploy_commit <> '');
