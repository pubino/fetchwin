#!/bin/zsh
# Run lint, format check, type check and tests.
#   scripts/test.sh            # locally (needs: scripts/bootstrap.sh)
#   scripts/test.sh --docker   # inside the pinned Linux containers (also runs the
#                              # PowerShell installer's Pester tests and analyzer)
set -euo pipefail

repo=${0:A:h:h}
cd "$repo"

if [[ ${1:-} == --docker ]]; then
  docker-compose build test
  docker-compose run --rm test
  docker-compose build test-pwsh
  docker-compose run --rm test-pwsh
  exit 0
fi

uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -v "$@"
