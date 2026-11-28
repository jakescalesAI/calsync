# CalSync

Keep your Google, Outlook, iCloud and other CalDAV calendars showing the same events. You run it yourself, and your sign-ins stay on your machine.

Every run makes each writable calendar hold one tagged copy of every real event from your other calendars. Published `.ics` links work as read-only sources, for calendars like Proton that have no way in.

- **Website:** https://getcalsync.app
- **Docs:** https://docs.getcalsync.app

## Run it

### Desktop app (Windows)

CalSync as an app: it runs on your computer in its own window, keeps syncing from the tray after you close the window, and can start when you sign in. There's no download yet, so you build the installer once. With Python 3.10+, Node 18+ and [Rust](https://rustup.rs/) installed:

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
```

If Windows Smart App Control is on, add `-Docker` (it needs WSL with Docker instead of Rust). Then run `desktop\out\CalSync_0.1.0_x64-setup.exe`. It installs for you only, without admin rights. The installer isn't signed yet, so if SmartScreen stops it, press **More info**, then **Run anyway**. Details, the tray menu and where your data lives are in [desktop/README.md](desktop/README.md). A Mac build is next.

### Docker

```bash
git clone https://github.com/jakescalesAI/calsync.git
cd calsync
docker compose up -d
```

Open http://localhost:8484. There's no account and no password: CalSync runs on your computer and only that computer can reach it. Add your calendars, press **Preview changes**, then **Apply and keep syncing**.

Google and Microsoft are one click: **Sign in with Google** or **Sign in with Microsoft**, in the browser on the computer CalSync runs on.
On a server you reach from elsewhere, use a free sign-in app of your own instead; the page walks you through it in about 10 minutes. iCloud, Fastmail, Yahoo, AOL, Zoho, Nextcloud and any other CalDAV server only need an app password.

### Python

Without Docker, using Python 3.10 or newer:

```bash
pipx install git+https://github.com/jakescalesAI/calsync.git
calsync web
```

On Windows, get pipx first, then open a new terminal and run the two lines above:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

## Booking apps

Calendly, Cal.com and other booking apps need nothing extra. Connect the app to any calendar CalSync keeps in sync. It
sees your busy times from every other calendar, and each new booking is copied out to the rest.

## AI agents

CalSync has no AI in it, but any agent that speaks MCP can use it: Claude Code, Hermes and others. `calsync mcp` lets an agent see all your calendars as one list. It's read only unless you add `--allow-write`, and no tool can sign in, start syncing or allow a mass delete. Those limits are on the tools: an agent that can run commands as you can still reach CalSync's data folder. Setup is in the [docs](https://docs.getcalsync.app/agents/).

## What keeps your calendars safe

- It only edits or deletes events it created. Each copy carries a private tag.
- Nothing changes until you've seen a preview and pressed start.
- If a calendar can't load, nothing is deleted because of it.
- If a run would delete more than half of a calendar's copies, it stops and waits for you.
- `calsync purge`, or the button on the page, removes every copy it made, so leaving is clean.

## What it does

- **Rules for each direction:** Work to Personal can copy titles and details while Personal to Work says only Busy. Pick
  what to copy, a tag and a colour, and leave out free, Maybe or unanswered events, all-day events, words, colours, days or hours.
  Private events always say Busy.
- **Why:** the Preview says why each event is or isn't copied. An Activity page lists every run.
- **A full calendar:** day, week and month; add, change and drag events, with Undo; RSVP and Join; search.
- **No double invites:** a meeting already on a calendar through an invite isn't copied again. Declined and cancelled events are skipped.
- **No triple buzz:** copies have no reminders.
- **No state to lose:** the tags are the state.
- **Quick add:** `calsync new Dentist tomorrow 3pm 1h`.
- **Notes (optional):** a Markdown agenda and a quick-add note, for Obsidian-style folders.

## Known limits (beta)

CalSync is in beta. What to know before you rely on it:

- **Google shows an "unverified app" warning for now.** Google is still reviewing CalSync's app. Click Advanced, then Go to CalSync, to go on. Until the review is done, at most 100 people can sign in with CalSync's app; after that, use your own sign-in app (Advanced on the Connect page).
- **School and work Microsoft accounts are often blocked** from outside apps. You can still bring them in read only, through the calendar's published `.ics` link.
- **Repeating events are copied as single occurrences**, not as one repeating series.
- **It looks 120 days ahead by default.** Change the window in Settings. Events further out are copied as they come into range.
- **Tested on Linux, WSL and Docker.** Windows and macOS without Docker work but are only lightly tested so far.

Found a problem? Open an [issue](https://github.com/jakescalesAI/calsync/issues). For anything about security, see [SECURITY.md](SECURITY.md).

## Command line

```text
calsync web                          web page and background sync
calsync setup google|microsoft       store your sign-in app
calsync auth google|outlook|ics NAME sign in an account
calsync auth caldav NAME [SERVER]    iCloud, Fastmail or any CalDAV server (icloud, fastmail, yahoo, aol, zoho, or an address)
calsync calendars                    list calendars on your accounts
calsync add NAME ACCOUNT CAL_ID [--busy-only] [--read-only] [--free] [--only TEXT] [--skip OTHER]
calsync remove NAME
calsync run [--dry-run] [--force]    one sync pass, for cron or a systemd timer
calsync purge [NAME] [--dry-run]     delete the copies calsync made
calsync status
calsync new TEXT [--check]
calsync mcp [--http] [--allow-write] [--busy-only]   tools for AI agents
calsync mcp token [--new]            print the --http token, or replace it
```

Settings live in `$CALSYNC_HOME`, by default `~/.calsync`. A sample systemd timer is in [`deploy/`](deploy/). Optional notes features are described in the [docs](https://docs.getcalsync.app).

## Develop

```bash
python3 -m unittest
```

The engine (`calsync.py`) and web page (`calsync_web.py`) use only the standard library, plus `python-dateutil` for repeating `.ics` events.

## License

[MIT](LICENSE)
