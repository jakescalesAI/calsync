# CalSync desktop app

CalSync as an app you install. It runs the engine and the setup page on your own computer and shows the page in an app window, with a tray icon (a menu-bar icon on a Mac).

- **Windows:** builds, installs and runs. Tested on Windows 11 with Smart App Control on.
- **macOS:** the code is written for it (menu-bar icon, LaunchAgent for Start on login), but it hasn't been built or tested on a Mac yet.
- **Linux:** not planned yet. `calsync web` or Docker already work there.

## Install on Windows

There's no download yet, so you build the installer once. You need [Python 3.10+](https://www.python.org/downloads/) and [Node 18+](https://nodejs.org/), then one of:

- **[Rust](https://rustup.rs/)** with the MSVC toolchain:

  ```powershell
  powershell -ExecutionPolicy Bypass -File desktop\build.ps1
  ```

- **WSL with Docker**, if Windows **Smart App Control** is on. Smart App Control blocks the small programs Rust builds and runs while compiling ("An Application Control policy has blocked this file", os error 4551), so Rust can't compile on that PC at all. This builds the app in a Linux container instead:

  ```powershell
  powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -Docker
  ```

Either way the installer lands in `desktop\out\CalSync_0.1.0_x64-setup.exe` (about 9 MB). Run it: it installs for you only, needs no admin rights, and adds CalSync to the Start menu. The installer isn't signed yet, so Windows may warn about it. If SmartScreen says "Windows protected your PC", press **More info**, then **Run anyway**.

## Using it

Open CalSync from the Start menu. It opens straight on the Sync tab, with no password: the page only answers on this computer. Add your calendars there, the same as on the web page.

The tray icon's menu has:

| Item | What it does |
| --- | --- |
| **Open CalSync** | Brings the window back. A left click on the icon does the same. |
| **Sync now** | Runs a sync straight away. Available once you've started syncing on the page, so it never skips the preview. |
| **Pause syncing** / **Resume syncing** | Stops the background runs until you resume, even after a restart. Sync now still works. |
| Last sync, problems | For example "Last sync 3 min ago" and "Problem: work: list failed: HTTP Error 404". The tooltip says the same in short. |
| **Keep syncing in the background** | On (the default): closing the window leaves CalSync syncing in the tray, and the first time a notification says so. Off: closing the window quits. |
| **Start on login** | Starts CalSync in the tray when you sign in, with no window, so it syncs without you opening it. Off until you turn it on. |
| **Quit CalSync** | Stops syncing and closes the app. |

If a sync that was fine starts having problems, Windows shows a notification once.

**Sign in with Google** and **Sign in with Microsoft** open your normal browser, not the app window. When you've allowed CalSync there, the browser comes back to the app's page on 127.0.0.1 and the app window moves on by itself.

From a terminal or a script, `CalSync.exe --tray <item>` does what that menu item does in the running app: `open`, `sync`, `pause` (toggles), `background` (toggles), `login` (toggles) or `quit`.

## Where things live

- **The program:** `%LOCALAPPDATA%\CalSync` (`CalSync.exe`, and the engine in `engine\`).
- **Your data:** `%LOCALAPPDATA%\app.getcalsync.desktop`. The engine's settings, sign-ins and last run are in `data\`, the app's own settings in `desktop.json`, and the log in `logs\calsync.log`. It's the local app-data folder, so tokens don't roam with your profile. Uninstalling keeps it unless you tick "Delete the application data".
- **The page:** `http://127.0.0.1:8495`, on 127.0.0.1 only, so nothing else on your network can reach it. If 8495 is taken, it uses the next free port up to 8520 and keeps it.

### Next to a CalSync in WSL or Docker

The app never uses port 8484, the one `calsync web` and the Docker image use. A CalSync in WSL answers on Windows' `127.0.0.1:8484` too, and the two stay separate: the app has its own data folder and port. Don't let both sync the same calendars, or each will copy the other's events.

### The command line

The engine that came with the app takes the commands from the main README, and finds the app's data folder by itself:

```powershell
& "$env:LOCALAPPDATA\CalSync\engine\calsync-engine.exe" status
```

## How it's built

- **Tauri 2**, the same as Quartzi's desktop app. The window is the system web view (WebView2 on Windows), and the app itself is a small Rust program (`src-tauri/`, 3.8 MB). It uses Tauri's tray icon, the autostart plugin (Start on login: a per-user Run entry on Windows, a LaunchAgent on a Mac), single instance (opening it again brings up the running one) and an NSIS per-user installer.
- **The engine** is `sidecar/calsync_desktop.py`, built with PyInstaller into `calsync-engine.exe` and bundled as a resource. It's `calsync web`, with the engine, the page and its scheduler unchanged. Pause only stops the scheduler's own runs (the interval, the one-minute check and push), so Sync now and syncs started from the page still work. The app talks to it over stdin and stdout, so there's no second port to guard. When the app exits or crashes, stdin closes and the engine exits too. If the engine stops by itself, the app restarts it, up to five times in ten minutes.
- **Why Tauri and not Electron:** the whole installer is about 9 MB, engine included. Electron would add about 80 MB of Chromium. The tray, autostart and installer pieces are the ones Quartzi already ships on Windows. Python stays the only engine, so the app, `calsync web` and Docker share one sync code path.
- **`-Docker` builds** compile for `x86_64-pc-windows-gnu` with mingw-w64, using only open-source tools (`cross/`). The MSVC build (plain `build.ps1`, and CI in `.github/workflows/desktop.yml`) is the usual one. Both make the same app.

## Develop

```powershell
cd desktop
npm install
$env:CALSYNC_DEV_PYTHON = "python"                    # run the sidecar script with your Python, no PyInstaller
$env:CALSYNC_DESKTOP_HOME = "$env:TEMP\calsync-dev"   # keep test data out of your real folder
npx tauri dev
```

`CALSYNC_DESKTOP_HOME` also works with an installed app, to try it against a throwaway data folder.

Tests: `build.ps1` runs them after every build (`-Docker` builds them for Windows and runs them on the PC). `-SkipEngine` reuses the last engine build, `-EngineOnly` builds only the engine, and `-Debug` makes a debug build (MSVC only).

Not done yet: signing the installer and the Mac app, notarizing, auto-update, a GitHub release, and a Mac build. Each needs Jake's OK.
