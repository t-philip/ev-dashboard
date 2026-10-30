# EV Dashboard

A personal, self-hosted, **read-only** dashboard for your own electric vehicle's data:
battery and range over time, charging sessions, trips and efficiency, and current status.

> **Status: in development. There is no release yet.** Nothing in this repository is
> installable today. See [`docs/DESIGN_SPEC.md`](docs/DESIGN_SPEC.md) for the design and for
> exactly what is and is not built or verified.

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
- **Location is opt-in.** If it is offered at all, it is off unless you turn it on.
- **Polite to the API.** Polling is designed around a fixed daily request budget below the
  manufacturer's limit.

## What you will need (once there is a release)

- A machine to run it on (a small Linux server or container is enough).
- A developer account and application with the vehicle manufacturer, with a redirect URI on a
  hostname you control, served over HTTPS.
- A vehicle that the manufacturer's Connected Vehicle, Energy and Location APIs support. The data
  available differs by model, year and region.

## Documents

- [Terms and Conditions](https://t-philip.github.io/ev-dashboard/terms.html)
- [Design specification (draft)](docs/DESIGN_SPEC.md)
- [Privacy rule for contributions](docs/PRIVACY.md)

## Licence

Licensed under [AGPL-3.0](LICENSE). Please credit **t-philip** if you use or share this.

## Author

T. Philip - [github.com/t-philip](https://github.com/t-philip)
