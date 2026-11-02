# calsync

Keep your Google and Outlook calendars showing the same events. You run it yourself, and your sign-ins stay on your machine.

Every run makes each writable calendar hold one tagged copy of every real event from your other calendars. Published `.ics` links work as read-only sources.

- **Website:** https://calsync.co
- **Docs:** https://docs.calsync.co

## Run it

```bash
git clone https://github.com/calsync-co/calsync.git
cd calsync
docker compose up -d
docker compose logs calsync
```

Open http://localhost:8484 and enter the setup code from the logs. Choose a password, add your calendars, press **Preview changes**, then **Apply and keep syncing**.

Google and Microsoft each need a free sign-in app of your own. The page walks you through it in about 10 minutes.

Without Docker, using Python 3.10 or newer:

```bash
pipx install git+https://github.com/calsync-co/calsync.git
calsync web
```

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
calsync setup google|microsoft       store your sign-in app
calsync auth google|outlook|ics NAME sign in an account
calsync calendars                    list calendars on your accounts
calsync add NAME ACCOUNT CAL_ID [--busy-only] [--read-only] [--skip OTHER]
calsync remove NAME
calsync run [--dry-run] [--force]    one sync pass, for cron or a systemd timer
calsync purge [NAME] [--dry-run]     delete the copies calsync made
calsync status
calsync new TEXT [--check]
```

Settings live in `$CALSYNC_HOME`, by default `~/.calsync`. A sample systemd timer is in [`deploy/`](deploy/). Optional notes features are described in the [docs](https://docs.calsync.co).

## Develop

```bash
python3 -m unittest
```

The engine (`calsync.py`) and web page (`calsync_web.py`) use only the standard library, plus `python-dateutil` for repeating `.ics` events.

## License

[MIT](LICENSE)
