#!/bin/zsh
# Install the macOS toolchain (Brewfile) and the Python dev environment (.venv via uv).
# Safe to re-run. --dry-run shows what would change without changing anything.
set -euo pipefail

repo=${0:A:h:h}
dry_run=0
for arg in "$@"; do
  case $arg in
    --dry-run) dry_run=1 ;;
    -h|--help) print "usage: ${0:t} [--dry-run]"; exit 0 ;;
    *) print -u2 "unknown argument: $arg"; exit 2 ;;
  esac
done

cd "$repo"

if ! command -v brew >/dev/null; then
  print -u2 "Homebrew is required: https://brew.sh"
  exit 1
fi

if (( dry_run )); then
  print "== brew bundle check (dry run)"
  command brew bundle check --verbose --file="$repo/Brewfile" || true
  print "== would run: uv sync --frozen"
  exit 0
fi

command brew bundle --file="$repo/Brewfile"
uv sync --frozen
print "Ready. Try: uv run fetchwin list"
