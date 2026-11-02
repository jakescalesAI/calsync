# Security

calsync holds sign-ins to your calendars, so treat its data folder like a password file.

- Sign-ins, client secrets and .ics links live in `secrets/` inside the data folder, readable only by its owner. They're stored unencrypted, the same as most self-hosted tools, so protect the machine and its backups.
- The web page listens on this machine only by default. To reach it from elsewhere, put it behind your own HTTPS proxy or a private network, and set `CALSYNC_HOSTS` and `CALSYNC_PUBLIC_URL`.
- The first visit needs the setup code from the logs (unless you're on the same machine, outside Docker), so a stranger can't claim a fresh install.
- calsync only edits or deletes events it created, and only talks to Google, Microsoft and the .ics links you add. It sends nothing anywhere else.

Please report a security problem privately through the repository's security advisories, not in a public issue.
