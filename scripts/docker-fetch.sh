#!/bin/zsh
# Run fetchwin inside the pinned Linux toolchain container. Arguments are passed through.
#   scripts/docker-fetch.sh fetch --arch x64 --channel business --dry-run
# ISOs land in ./out (override with FETCHWIN_HOST_OUT=/some/dir).
set -euo pipefail

repo=${0:A:h:h}
cd "$repo"

export FETCHWIN_UID=$(id -u) FETCHWIN_GID=$(id -g)
export FETCHWIN_HOST_OUT=${FETCHWIN_HOST_OUT:-$repo/out}
mkdir -p "$FETCHWIN_HOST_OUT"

docker-compose build fetchwin
docker-compose run --rm fetchwin "$@"
