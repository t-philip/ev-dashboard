# EV Dashboard

A personal, self-hosted, **read-only** dashboard for your own electric vehicle's data:
battery and range over time, charging sessions, trips and efficiency, and current status.

> **Status: early pre-release (0.2.x).** It collects and stores readings and shows the current
> state of one vehicle, and, if you turn location on, a browsable history of trips (see *Trips*
> below). Charts and charging-session detection are not built yet. It has been run against one
> vehicle only. See [`docs/DESIGN_SPEC.md`](docs/DESIGN_SPEC.md)
> for exactly what is and is not built or verified.

Independent hobby project. Not affiliated with, endorsed by, or sponsored by Volvo Cars or any
other vehicle manufacturer.

## Principles

- **You run it, your data stays with you.** There is no hosted service. Readings will be stored in
  a database on the machine where you run the application, and the design sends nothing to the
  author or to any third party.
- **Read-only.** It is designed to request read scopes only, so it cannot lock, unlock, start,
  stop or otherwise control the vehicle.
- **Bring your own credentials.** Every user registers their own developer application with the
  manufacturer and signs in with their own account. No credentials are shipped with this project.
- **Location is opt-in.** It is off unless you turn it on, it is stored only on your machine, and
  you set how long it is kept.
- **Polite to the API.** Polling is designed around a fixed daily request budget below the
  manufacturer's limit.

## What you will need

- A machine to run it on (a small Linux server or container is enough).
- A developer account and application with the vehicle manufacturer, with an HTTPS redirect address
  that your own browser can reach (it can be on a private network; see *First run*).
- A vehicle that the manufacturer's Connected Vehicle, Energy and Location APIs support. The data
  available differs by model, year and region.

## Running it

Python 3.12 or newer, standard library only. The test suite (`PYTHONPATH=src python -m unittest
discover -s tests`, run from the repository root) has been run locally on 3.12 and 3.13; there is no
CI yet. Run the application from the repository root with `PYTHONPATH=src python -m evdash <command>`:

| Command | What it does |
|---|---|
| `serve` | The web page: sign-in, and the latest value of every field with its age. **It has no login of its own and shows live vehicle state: keep it on loopback (the default) or behind a VPN or similar private network. Binding it to any non-loopback address (`0.0.0.0` or a LAN address) exposes it to everything that can reach that address.** |
| `collect` | Polls the APIs on a schedule and stores readings in SQLite, until stopped |
| `poll` | One polling pass; prints counts only |
| `probe [PATH...]` | Requests endpoints once and prints each reply's shape (field names, types and short status words, never values) |
| `status` | Whether you are signed in and which scopes were granted |
| `refresh` | Refreshes the stored sign-in if it is due |

Run `serve` and `collect` as two services sharing one database. Settings are environment variables:

| Variable | Meaning |
|---|---|
| `EVDASH_REDIRECT_URI` | Your HTTPS redirect address, registered with the manufacturer (required) |
| `EVDASH_SCOPES` | The read scopes to request, space-separated (required) |
| `EVDASH_LISTEN` | `host:port` for the web page (default `127.0.0.1:8100`) |
| `EVDASH_TOKEN_FILE` / `EVDASH_DB` | Where the sign-in tokens and the readings are stored (default under `data/`) |
| `EVDASH_DAILY_BUDGET` | Most requests per UTC day (default 2000; the cap of 9000 is a safety margin under the manufacturer's documented default limit of 10,000 per API per day) |
| `EVDASH_VIN` | Vehicle to use; if unset, the first vehicle on the account |
| `EVDASH_LOCATION` | `1` to collect location and show the trips pages; off by default. Needs `location:read` in `EVDASH_SCOPES` and a new sign-in |
| `EVDASH_LOCATION_RETENTION_DAYS` | How long location readings are kept (default 365, at most 3650). Older ones are deleted once a day and the database file is then compacted (copies in backups or synced folders are not touched) |
| `EVDASH_TIMEZONE` | Time zone name for the times on the trips pages (default `UTC`; needs the system's time zone data for any other) |

Credentials are read from files, never from environment values: `client_id`, `client_secret` and
`vcc_api_key`, either in the directory named by `CREDENTIALS_DIRECTORY` or at the paths in
`EVDASH_CLIENT_ID_FILE`, `EVDASH_CLIENT_SECRET_FILE` and `EVDASH_VCC_API_KEY_FILE`. On POSIX systems the
application writes the token file readable by its owner only; the database takes its permissions from the
process umask, so run the services with `UMask=0077` or equivalent. Keep `data/` and the credential
files out of version control and out of shared backups. Your vehicle identification number is a
personal identifier: `status` and `probe` never print it and the collector logs only its last four
characters, but do not paste it into issues. Deleting the database file deletes every stored reading.

### Trips

With `EVDASH_LOCATION=1` the web page gains a **Trips** section: a month-by-month list and a page
per trip. The manufacturer's documentation says its location API reports a position only when a
trip ends and is not a history, so a *trip* here is the move between two consecutive reported stops
(not yet verified against real trips). Each shows where it
started and ended, the odometer distance, the battery change (flagged if the car was charged in
between) and, when it was observed, the start time. **Routes are not available**: the maps show your
stops to scale on a plain background, with a dashed straight line between a trip's two stops, and are
drawn without any map service, so no coordinate leaves your machine when you view them. Each trip's
page has two "show on a map" links to openstreetmap.org; nothing is sent unless you click one. History
starts the day collection starts, and only the account's first vehicle is shown.

**The trips pages show precise positions and, like the rest of the web page, have no login:** the same
network-boundary rule applies, and it matters more here.

### First run

1. Register an application on the manufacturer's developer portal, choosing the **read** scopes only,
   and note the client id, client secret and application key.
2. Choose a redirect address: an HTTPS URL ending in `/auth/callback` that **your own browser** can
   reach. It may sit on a private network (for example a private overlay network's HTTPS name);
   it does not need to be reachable from the internet. Put a TLS-terminating front in front of the
   loopback port, and keep that front off the public internet (or add authentication to it): the
   page behind it has none. Only `/auth/callback` has to be reachable, and only from your own browser.
3. Set `EVDASH_SCOPES` to the scopes you registered and need, for example:
   `openid conve:vehicle_relation conve:battery_charge_level conve:odometer_status
   conve:trip_statistics conve:engine_status conve:lock_status conve:doors_status
   conve:windows_status conve:tyre_status conve:brake_status conve:warnings
   conve:diagnostics_workshop energy:state:read energy:capability:read`. Requesting a scope the
   application was not registered with makes sign-in fail.
4. Start `serve`, open its page, press **Sign in**, and approve. You are redirected back and the
   page then shows which scopes were granted.
5. Run `collect` as a second service (for example a systemd unit that restarts on failure). The
   sign-in stays alive only while it is used: according to the manufacturer's documentation a refresh
   token unused for seven days becomes invalid. The collector refreshes the access token whenever it
   is due; if you stop it for longer than that, run `refresh` within the window or sign in again.

The data is the manufacturer's to withdraw: it may change or end access at any time. The licence is
AGPL-3.0, whose network clause applies if you offer a modified version to others. Release notes are
on the repository's releases page.

## Documents

- [Terms and Conditions](https://t-philip.github.io/ev-dashboard/terms.html)
- [Design specification (draft)](docs/DESIGN_SPEC.md)
- [Privacy rule for contributions](docs/PRIVACY.md)

## Licence

Licensed under [AGPL-3.0](LICENSE). Please credit **t-philip** if you use or share this.

## Author

T. Philip - [github.com/t-philip](https://github.com/t-philip)
