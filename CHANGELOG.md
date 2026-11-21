# Changelog

All notable changes to calsync. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Two-way CalDAV calendars: Apple iCloud, Fastmail, Yahoo, AOL, Zoho, Nextcloud and any other CalDAV server, signed in
  with an app password. Basic and Digest sign-in, server discovery, event calendars only (Reminders lists are skipped).
- `.ics` sources now honour RDATE, and keep calsync's own tag so a published copy is never copied again.
- Windows: the engine no longer needs `fcntl`, reads the time zone from the system, and prints any event title.
- The connect page explains how booking apps such as Calendly and Cal.com work with calsync.
- `calsync add --free` stops a feed's items marking you busy, and `--only TEXT` keeps only items whose UID holds that
  text. Both are for due-date feeds such as school assignments. A due time with no length now shows as 30 minutes.
- `calsync mcp`: tools for AI agents over MCP, on stdin and stdout or over HTTP with a bearer token. Read only unless you
  add `--allow-write`; `--busy-only` hides titles and places. No tool can sign in, purge, remove a calendar, start syncing or force a mass delete.
- A skill for Hermes that installs and sets up calsync with you, connects these tools and uses them, at `docs-site/hermes/SKILL.md`.

### Changed
- The setup page has the look of getcalsync.app: the calendar mark in a proper header with the current page marked, each
  calendar as a row with its flags as chips, the preview's counts as tiles, light and dark themes, and a layout that fits
  a phone. Still no scripts, no inline styles and no outside files; every form, field and link is unchanged.

### Fixed
- The delete page says "1 copy" instead of "1 copies".
- A calendar read through a published `.ics` link no longer has calsync's old copies copied a second time. A feed such as
  Google's secret address drops the tag, so a copy is now also known by the note it carries.
- The setup page's forms work in a browser again. The page sent `Referrer-Policy: no-referrer`, which makes a browser
  send `Origin: null` with the page's own forms, so each one was refused as coming from another site. It now sends
  `same-origin`. The origin check is unchanged.

### Security
- A CalDAV password follows only the host it was typed for, a host under it, or a built-in provider's own hosts.
  Before, any host sharing the last two parts of the name counted, such as another name on `co.uk`.
- A hostile invite or feed can't hang or stop a run: rules repeating every minute or second are left out, an event
  that can't be read is skipped, and a calendar that's too slow or over 50 MB is reported with its copies left alone.
- A calendar link with a space is refused, so its secret part can't land in an error message.
- Busy and private copies no longer name the calendar they came from.
- The web page drops connections that go quiet for 30 seconds, allows 64 at once, and caps guesses at the current
  password in Settings.
- CI actions are pinned to exact commits.

## [0.1.0] - 2026-11-03

First public release.

### Added
- Two-way sync between Google Calendar and Outlook / Microsoft 365 calendars. Each writable calendar gets one
  tagged copy of every real event from the others; published `.ics` links work as read-only sources.
- Web setup page (`calsync web`, port 8484): first-run password, sign-in guides for Google and Microsoft,
  Preview changes before anything is written, background sync every 2 to 60 minutes, Pause, Purge.
- Busy-only calendars, skip lists, no double invites, no reminders on copies, private events copied as Busy.
- Safety stops: a calendar that fails to load is never deleted from; a run that would remove more than half of a
  calendar's copies stops and waits.
- Command line: `setup`, `auth`, `calendars`, `add`, `remove`, `run`, `purge`, `status`, `new` (quick add).
- Optional Markdown features: quick-add note, agenda note and daily-note Calendar sections, `.ics` export.
- Docker image (non-root, read-only root filesystem) and `compose.yaml` publishing the page on this machine only.

### Security
- The first visit needs a setup code from the logs unless it comes from this machine directly; a proxy, a public
  address, another host name or the Docker image always need it.
- Wrong passwords and setup codes are capped per address and across all addresses.
- Sign out on every device. Strict content policy: the page has no JavaScript and no outside files.

[Unreleased]: https://github.com/jakescalesAI/calsync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jakescalesAI/calsync/releases/tag/v0.1.0
