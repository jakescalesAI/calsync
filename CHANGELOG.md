# Changelog

All notable changes to calsync. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
