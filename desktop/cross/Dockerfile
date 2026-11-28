# Builds the Windows app from Linux (WSL or any Docker host): Rust for x86_64-pc-windows-gnu with
# mingw-w64, and NSIS for the installer. Used by build.ps1 -Docker on PCs where Smart App Control
# blocks Rust's build scripts. Only open-source tools: no Microsoft SDK download or license.
# trixie: Tauri's installer template needs NSIS 3.09 or newer (bookworm has 3.08).
FROM rust:1-trixie
# libayatana-appindicator3-dev: the tauri CLI looks for it whenever tray-icon is on, even for a Windows target.
RUN apt-get update \
 && apt-get install -y --no-install-recommends mingw-w64 nsis nodejs npm rsync libayatana-appindicator3-dev pkg-config \
 && rm -rf /var/lib/apt/lists/*
RUN rustup target add x86_64-pc-windows-gnu
WORKDIR /work
