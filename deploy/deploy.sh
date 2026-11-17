#!/usr/bin/env bash
# Deploy the committed school-api to the host with git: only objects the host does not have travel, and the host
# checks out only what changed (files removed in git are removed there too). Server-only things (.env, data/, run/,
# secrets/, backups/) are ignored by git and never touched. What runs afterwards depends on what changed:
#   Dockerfile, pyproject.toml, uv.lock, app/  -> build the image (Docker's cache keeps unchanged layers), restart
#   sql/                                       -> migrate
#   compose*.yml                               -> docker compose up -d
# docker compose on the host uses COMPOSE_FILE from its .env when set (e.g. compose.gpu.yml, or a compose.local.yml
# that is not in git).
#   only docs                                  -> nothing to restart
# Usage: deploy/deploy.sh <ssh host> [commit]   (commit defaults to HEAD). Redeploying an older commit = rollback.
set -euo pipefail
HOST="${1:?usage: deploy/deploy.sh <ssh host> [commit]}"
REV="${2:-HEAD}"
DEST=/srv/school-api
REPO=/srv/school-api.git
cd "$(git rev-parse --show-toplevel)"

# only committed state: what runs on the host is always one commit of git
if [ "$REV" = HEAD ] && [ -n "$(git status --porcelain)" ]; then
  echo "there are uncommitted changes; commit them first:" >&2
  git status --short >&2
  exit 1
fi
SPLIT=$(git rev-parse --verify "$REV^{commit}")

# the host's bare repository and its checkout (once)
ssh "$HOST" "test -d $REPO || git init -q --bare $REPO"
git push -q "ssh://$HOST$REPO" "$SPLIT:refs/heads/deploy" --force

ssh "$HOST" "set -euo pipefail
  cd $DEST
  OLD=\$(cat .deployed 2>/dev/null || true)
  git --git-dir=$REPO --work-tree=$DEST checkout -q -f deploy
  NEW=\$(git --git-dir=$REPO rev-parse deploy)
  if [ -n \"\$OLD\" ] && git --git-dir=$REPO cat-file -e \"\$OLD^{commit}\" 2>/dev/null; then
    CHANGED=\$(git --git-dir=$REPO diff --name-only \"\$OLD\" \"\$NEW\")
  else
    CHANGED=ALL   # first deploy with git (or unknown previous state): do everything
  fi
  test -f .env || { echo 'first run deploy/setup-host.sh on the host'; exit 1; }
  has() { [ \"\$CHANGED\" = ALL ] || printf '%s\n' \"\$CHANGED\" | grep -qE \"\$1\"; }
  echo \"\${OLD:0:12} -> \${NEW:0:12}\"; [ \"\$CHANGED\" = ALL ] && echo '  changed: (all)' || printf '  changed: %s\n' \$CHANGED
  if has '^(Dockerfile|pyproject.toml|uv.lock|app/)'; then echo '  build';   docker compose build -q api; fi
  if has '^sql/';                                     then echo '  migrate'; docker compose up -d db && docker compose run --rm api school-api migrate; fi
  if has '^(Dockerfile|pyproject.toml|uv.lock|app/|compose[^/]*\\.yml)'; then echo '  restart'; docker compose up -d --remove-orphans; fi
  echo \"\$NEW\" > .deployed
  docker compose ps --format '  {{.Service}}: {{.Status}}'"
