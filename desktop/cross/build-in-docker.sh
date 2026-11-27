#!/bin/sh
# Runs inside the cross image. /src is the repo (read only), /out gets the installer.
set -eu
rsync -a --delete --exclude node_modules --exclude src-tauri/target --exclude .build --exclude out /src/desktop/ /work/desktop/
mkdir -p /work/brand && rsync -a /src/brand/ /work/brand/
cd /work/desktop
[ -d node_modules/@tauri-apps/cli ] || npm install --no-audit --no-fund
npx tauri build --target x86_64-pc-windows-gnu --bundles nsis
cp src-tauri/target/x86_64-pc-windows-gnu/release/bundle/nsis/*.exe /out/
# The unit tests, built for Windows; build.ps1 runs them on the PC.
tests=$(cargo test --manifest-path src-tauri/Cargo.toml --release --target x86_64-pc-windows-gnu --lib --no-run --message-format=json 2>/dev/null \
  | sed -n 's/.*"executable":"\([^"]*\)".*/\1/p' | tail -1)
cp "$tests" /out/calsync-desktop-tests.exe
ls -l /out
