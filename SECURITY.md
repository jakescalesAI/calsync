# Security

CalSync holds sign-ins to your calendars, so treat its data folder like a password file.

- Sign-ins, CalDAV passwords, client secrets and .ics links live in `secrets/` inside the data folder, readable only by its owner. On Windows that protection comes from your user folder, so if you point `CALSYNC_HOME` somewhere else, check who can read it. They're stored unencrypted, the same as most self-hosted tools, so protect the machine and its backups.
- The web page has no password: it's yours because only your computer can reach it. It listens on 127.0.0.1 and answers only to `localhost` or `127.0.0.1` with its own port, so a web site can't reach it by pointing a name of its own at your computer (DNS rebinding). A change is taken only when the request's Origin or Referer is the page itself and it carries the page's form token, which is tied to your browser. It sends no CORS headers.
- Anyone who can run programs as you on this computer can open the page, as they could read CalSync's folder anyway.
- It refuses to listen on any other address unless `CALSYNC_ALLOW_REMOTE=1` is set too, and then it warns that anyone who can reach that address can read and change your calendars. The Docker image needs it, since a container listens on its own network; `compose.yaml` sets it and publishes the port on 127.0.0.1 only. Don't publish it more widely without a private network or a proxy that signs you in.
- An older install's page password file (`secrets/web.json`) is left where it is and no longer read.
- A CalDAV password only goes to the server you typed, or a host under it, and never from `https://` down to `http://`. Only the built-in providers can move it between their own hosts. If your server redirects somewhere else, CalSync stops; type the final address instead. `http://` works only for a server on your own network, and the password crosses that network unencrypted.
- Anyone can put an event on your calendar by inviting you, and a published calendar can be anyone's. An event CalSync can't read is skipped, a rule repeating every minute or second is left out, and a calendar that's too big or too slow to read is reported with its copies left alone.
- A Busy copy shows no title or location, and doesn't name the calendar it came from.
- CalSync only edits or deletes events it created, marked with its own tag. On a CalDAV calendar someone else could send an invite carrying that tag, and CalSync would treat it as its own copy and remove it.
- `calsync mcp` is read only unless you add `--allow-write`. Over HTTP it listens on this machine only and needs a bearer token, kept in `secrets/`, on every request; it refuses a web page's request. Its tools can't sign in, purge, remove a calendar, start or resume syncing, or force a mass delete, and never return a calendar ID, an account name, a token, a .ics link or a server's reply: a failure comes back as one of a few fixed reasons. With `--allow-write`, a sync they run can remove a few copies whose original is gone, as any sync does.
- An event title can be written to trick an AI agent, since anyone can invite you. `calsync mcp` cuts titles and places to one short line, removes control characters and tells the agent they are data. It can't promise what a model does, so give an agent `--allow-write` only if it asks you before it adds something. What an agent reads also goes to the model it runs on; `--busy-only` shows times only.
- `calsync mcp`'s limits apply to its tools only. An agent that can also run commands as you, as most local agents can, can read the data folder or run `calsync purge` directly. To stop that, turn off its shell, file, code and sub-agent tools, or run CalSync under another user and connect with `--http`.
- CalSync only talks to Google, Microsoft, your CalDAV servers and the .ics links you add. It sends nothing anywhere else.

## Reporting a problem

Please report a security problem privately, not in a public issue: use **Report a vulnerability** on the repository's
[Security tab](https://github.com/jakescalesAI/calsync/security/advisories/new). Only the maintainer sees it. Say what
you found and how to reproduce it; leave out real sign-ins, tokens and calendar links.
