# CalSync

Keep your Google, Outlook, iCloud and other CalDAV calendars showing the same events. You run it yourself, and your sign-ins stay on your machine.

Every run makes each writable calendar hold one tagged copy of every real event from your other calendars. Published `.ics` links work as read-only sources, for calendars like Proton that have no way in.

- **Website:** https://getcalsync.app
- **Docs:** https://docs.getcalsync.app

## Run it

```bash
git clone https://github.com/jakescalesAI/calsync.git
cd calsync
docker compose up -d
docker compose logs calsync
```

Open http://localhost:8484 and enter the setup code from the logs. Choose a password, add your calendars, press **Preview changes**, then **Apply and keep syncing**. Save the recovery code it shows you: if you forget the password, it lets you pick a new one on that computer, and so does `calsync reset-password`. There is no account and no email reset.

Google and Microsoft each need a free sign-in app of your own. The page walks you through it in about 10 minutes. iCloud, Fastmail, Yahoo, AOL, Zoho, Nextcloud and any other CalDAV server only need an app password.

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

- **Busy only:** copies on a chosen calendar say Busy, with no title or location. Private events always say Busy.
- **Skip lists:** for example, don't copy work to personal.
- **No double invites:** a meeting already on a calendar through an invite isn't copied again. Declined and cancelled events are skipped.
- **No triple buzz:** copies have no reminders.
- **No state to lose:** the tags are the state.
- **Quick add:** `calsync new Dentist tomorrow 3pm 1h`.
- **Notes (optional):** a Markdown agenda and a quick-add note, for Obsidian-style folders.

Limits: it looks 120 days ahead, and repeating events are copied as individual occurrences. Some school and work Microsoft accounts block outside apps. Those can still be read one way through a published `.ics` link.

## Command line

```text
calsync web                          web page and background sync
calsync reset-password               forgot the page password: choose a new one (on that computer only)
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
