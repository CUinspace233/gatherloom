#!/usr/bin/env bash
# Update this checkout to origin/main and restart the production stack.
#
#   scripts/deploy.sh            # run from anywhere on the server
#
# The deploy job in .github/workflows/server.yml runs it over SSH after the
# checks pass on main. Migrations run before the new version starts; the
# script fails if the API is not healthy afterwards.
set -euo pipefail

# Bash reads a script as it runs; the braces keep the pull from changing it midway.
{
  root="$(cd "$(dirname "$0")/.." && pwd)"
  env_file="$root/deploy/.env.prod"
  compose() { docker compose -f "$root/deploy/compose.prod.yaml" --env-file "$env_file" "$@"; }

  exec 9>"$root/.git/deploy.lock"
  flock -w 600 9

  cd "$root"
  git fetch --quiet origin main
  git checkout --quiet main
  git merge --quiet --ff-only origin/main
  echo "Deploying $(git log -1 --format='%h %s')"

  compose up -d --build --remove-orphans
  for _ in $(seq 60); do
    if compose exec -T api python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" 2>/dev/null; then
      docker image prune -f --filter label=com.docker.compose.project=gatherloom-prod >/dev/null
      compose ps
      echo "Deployed $(git rev-parse --short HEAD)"
      exit 0
    fi
    sleep 2
  done
  compose ps
  compose logs --tail 50 migrate api
  echo "The API did not become healthy." >&2
  exit 1
}
