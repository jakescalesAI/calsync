#!/bin/sh
# From WSL or Linux: builds the Windows installer in the cross image and puts it in desktop/out.
# build.ps1 -Docker calls this through WSL.
set -eu
repo=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$repo/desktop/out"
docker build -q -t calsync-desktop-cross "$repo/desktop/cross" >/dev/null
docker run --rm \
  -v "$repo":/src:ro \
  -v "$repo/desktop/out":/out \
  -v calsync-cross-cargo:/usr/local/cargo/registry \
  -v calsync-cross-work:/work \
  calsync-desktop-cross sh /src/desktop/cross/build-in-docker.sh
