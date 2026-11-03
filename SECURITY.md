# Security

calsync holds sign-ins to your calendars, so treat its data folder like a password file.

- Sign-ins, client secrets and .ics links live in `secrets/` inside the data folder, readable only by its owner. They're stored unencrypted, the same as most self-hosted tools, so protect the machine and its backups.
- The web page listens on this machine only by default. To reach it from elsewhere, put it behind your own HTTPS proxy or a private network, and set `CALSYNC_HOSTS` and `CALSYNC_PUBLIC_URL`.
- The first visit needs the setup code from the logs, so a stranger can't claim a fresh install. Only a visit from this machine itself, outside Docker, to `localhost` skips it; anything that comes through a proxy (a `Forwarded` or `X-Forwarded-For` header, `CALSYNC_PUBLIC_URL` set, or another host name) needs the code.
- Wrong passwords and setup codes are capped at 5 a minute per address, plus 30 (passwords) and 10 (setup codes) a minute from all addresses together. Someone who can reach the page can therefore keep it locked for a while; that's on purpose, since a lock-out is better than a guessable password. Keep the page off the open internet.
- Settings has **Sign out on every device**, which ends every session at once, for a lost laptop or a copied cookie.
- calsync only edits or deletes events it created, and only talks to Google, Microsoft and the .ics links you add. It sends nothing anywhere else.

Please report a security problem privately through the repository's security advisories, not in a public issue.
