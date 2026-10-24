# EV Dashboard - Design Specification

**Status: DRAFT (pre-implementation).** Nothing described below is built yet. This document records
the design decisions and the constraints behind them. Section 10 states which acceptance criteria
are verified (none yet). It will be reconciled against the as-built code before the first release.

## 1. Purpose and scope

A web dashboard, run by the vehicle owner on their own hardware, that turns the manufacturer's
latest-value vehicle APIs into **history and insight**: the APIs return only the most recent
reading for each field, so anything over time (range trends, charging sessions, trips, efficiency)
must be recorded by this application.

In scope for the first release:

1. A **collector** that polls the manufacturer's Connected Vehicle, Energy and Location APIs on a
   schedule and stores every reading with its timestamp.
2. A **current-state** view: battery, range, charging state, locks, doors, windows, tyre and
   service warnings, and the age of each value.
3. **Battery and range over time.**
4. **Charging sessions**, derived from changes in charging state.
5. **Trips and efficiency**, derived from odometer and engine-state changes.
6. **Location**, opt-in and off by default.

Out of scope: any command that controls the vehicle; any hosted or multi-user service; mobile
apps (a later, separate decision).

## 2. Constraints from the manufacturer's terms

The manufacturer's developer terms and API agreement, as published when this draft was written, shape the design:

| Constraint | Consequence for the design |
|---|---|
| Portal login and API licence are non-transferable and non-sublicensable | No shared key can ship. **Every user registers their own application.** |
| Request limit of 10,000 calls per day per API | A **poll budget** that is enforced in code, with a default well below the limit |
| Data must not be shared with third parties, and must be erased when no longer needed | **Self-hosted, one owner per install.** No telemetry, no hosted multi-user mode. Deleting the database removes the history. |
| No use of the manufacturer's names or logos | Neutral project name; the name appears only in factual statements |
| Documentation and the APIs may not be copied or modified | This repository links to the manufacturer's documentation and does not bundle it |
| The manufacturer may change or end API access at any time without notice | Every call fails gracefully; the dashboard keeps showing history and marks values as stale |
| Publishing an application is a manual review and requires a public terms page | The user supplies their own terms URL when registering. A template page is in `docs/terms.html` |

**Open point:** the API agreement states its purpose as offering products and services to
end-customers. It does not clearly address a personal, non-commercial, self-hosted tool. This is to be
clarified with the manufacturer before the first release.

## 3. Data available

Field availability varies by model, year and region, and the application treats every field as
optional. These are the groups the design relies on.

- **Energy API:** battery charge level, electric range, charger connection status, charging status
  and type, charger power status, estimated time to target, target charge level.
- **Connected Vehicle API:** odometer; trip meters; average energy consumption and average speed
  (the "automatic" variants); distance to empty (battery); engine status; central lock; doors,
  hood, tailgate; windows and sunroof; service due (distance, time, engine hours); tyre warning
  status (status only, no pressure values); brake-fluid and washer-fluid warnings.
- **Location API:** coordinates, altitude and heading.

Some fields can be reported as unsupported, unspecified or empty (for example charging limits,
light and fluid warnings, and some average-consumption values), and charging power may be present
only while charging. The application must handle all of these, and must not assume a field exists.

Every value carries a timestamp, which the application stores and shows as data age.

## 4. Architecture

```
manufacturer APIs  <-HTTPS-  collector  ->  SQLite  <-  web UI (read-only views)
                              |
                              +-- auth module (OAuth 2.0, tokens in a protected file)
```

- **Language and stack:** Python, SQLite, a small web framework with a server-rendered UI and
  lightweight charts. To be confirmed at implementation time.
- **One process or two:** a collector service and a web service that share the database. They are
  separate so a UI fault never stops collection.
- **Storage:** one table of raw readings (vehicle, field, value, unit, source timestamp, fetched-at)
  plus derived tables for charging sessions and trips. Raw readings are kept so derived tables can
  be rebuilt when detection logic improves.

## 5. Authentication

- **OAuth 2.0 authorisation-code flow with PKCE**, as the manufacturer requires for new
  applications.
- **Redirect URI:** an HTTPS address on a hostname the user controls, registered exactly. Matching
  is by exact string comparison, so the callback path is fixed (`/auth/callback`). Localhost is
  rejected by the manufacturer's registration form.
- **Scopes (read-only only):** the minimum for the features in section 1. No command, lock, unlock,
  engine, climate, honk/flash, navigation or wallbox-write scopes are ever requested. Location read
  is requested but the feature is off unless the user enables it.
- **Tokens:** access tokens are short-lived; refresh tokens rotate on every use, expire if unused
  for 7 days, and the grant ends after 6 months, when the owner must sign in again. Tokens are
  stored in a file outside the web root with owner-only permissions, are never logged or printed,
  and are written atomically so a crash cannot lose the rotated refresh token.
- **Callback page:** minimal, with no third-party scripts, because the authorisation code is in
  the URL.

## 6. Polling and the request budget

Each full poll costs roughly a dozen API calls. The collector assigns each field group an interval
(for example frequent for charging and lock state, rare for odometer and location) and **refuses to
exceed a configured daily budget**, defaulting well below the manufacturer's limit. When the budget is spent it stops and says so in the UI.
It backs off on errors and never retries in a tight loop.

## 7. Privacy and security

- No telemetry, analytics or outbound calls other than to the manufacturer's APIs.
- Location is opt-in; the vehicle identification number is treated as sensitive and masked in logs
  and in the UI by default.
- Secrets live only in a protected file or the environment, never in the repository. The repository
  carries an automated privacy check and a contribution rule; see [`PRIVACY.md`](PRIVACY.md).
- The web UI is intended for a private network and binds to a private interface by default.

## 8. Phases

1. Auth and collector, raw storage, current-state view.
2. History charts for battery and range.
3. Charging-session detection.
4. Trip detection and efficiency.
5. Location (opt-in).
6. Packaging, documentation, first release.

## 9. Design decisions

Direct use of the manufacturer's API; web first, Android possibly later; a public, self-hosted,
single-owner project.

## 10. Acceptance criteria and their status

| Criterion | Status |
|---|---|
| Collector stores every reading with its timestamp | Not built, not verified |
| Daily request budget is enforced in code | Not built, not verified |
| OAuth sign-in with PKCE completes and tokens refresh unattended | Not built, not verified |
| Refresh tokens are never logged or printed | Not built, not verified |
| Charging sessions detected correctly | Not built, not verified |
| Trips and efficiency detected correctly | Not built, not verified |
| Location is off unless enabled | Not built, not verified |
| A user can delete all stored data by deleting the database | Not built, not verified |
| Vehicle data fields listed in section 3 are returned by the APIs | Not yet covered by an automated test in this repository |
