"""Command line: ``python -m evdash serve | status | refresh | poll | collect | probe``."""
from __future__ import annotations

import json
import logging
import signal
import sys
import threading

from . import auth, collector, config, probe, redact, store
from .server import App, make_server


def refresh(manager: auth.TokenManager) -> int:
    """Refresh the stored sign-in if it is due. Prints facts only, never a token value."""
    try:
        manager.access_token()
    except auth.AuthError as err:  # messages carry a status and an error code, nothing secret
        print(f"refresh failed: {err}", file=sys.stderr)
        return 1
    print(f"sign-in is current; access token valid for {manager.status()['access_token_valid_for']} s")
    return 0


def collect(command: str, settings: config.Settings, manager: auth.TokenManager) -> int:
    """``poll``: one pass, counts only. ``collect``: run until stopped."""
    db = store.Store(settings.db_path)
    worker = collector.Collector(manager, db, settings.api_key, base=settings.api_base,
                                 budget=settings.daily_budget, vins=settings.vins,
                                 location=settings.location)
    try:
        if command == "poll":
            summary = worker.run_once()
            print("poll finished:", ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
            return 0 if summary["signed_in"] else 1
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        logging.getLogger("evdash").info("collecting (daily budget %d)", settings.daily_budget)
        worker.run_forever(stop)
        return 0
    finally:
        db.close()


def explore(settings: config.Settings, manager: auth.TokenManager, paths) -> int:
    """``probe [PATH...]``: what do the endpoints return? Shapes and status words only."""
    chosen = list(paths) or list(probe.DEFAULT_PATHS)
    db = store.Store(settings.db_path)
    try:
        worker = collector.Collector(manager, db, settings.api_key, base=settings.api_base,
                                     budget=settings.daily_budget, vins=settings.vins)
        for path, status, shape in worker.explore(chosen):
            if status is None:  # nothing was asked, or no reply came: ``shape`` then holds the reason
                print(f"{path} -> {shape if isinstance(shape, str) else 'no reply'}")
                continue
            print(f"{path} -> HTTP {status}")
            print("   ", json.dumps(shape, sort_keys=True))
        return 0
    finally:
        db.close()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""
    if command not in ("serve", "status", "refresh", "poll", "collect", "probe"):
        print("usage: python -m evdash serve | status | refresh | poll | collect | probe [PATH...]",
              file=sys.stderr)
        return 2
    try:
        settings = config.load(need_api_key=command in ("poll", "collect", "probe"))
    except config.ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2
    redact.install(known=(settings.oauth.client_id, settings.oauth.client_secret,
                          settings.api_key or "") + settings.vins)
    token_store = auth.TokenStore(settings.token_file)
    manager = auth.TokenManager(settings.oauth, token_store)

    if command == "status":
        info = manager.status()  # facts only, never a token value
        print("signed in" if info["signed_in"] else "not signed in")
        if info.get("granted_scopes"):
            print("granted scopes:", " ".join(info["granted_scopes"]))
        elif info["signed_in"]:
            print("granted scopes: not reported by the server")
        return 0

    if command == "refresh":
        return refresh(manager)

    if command in ("poll", "collect"):
        return collect(command, settings, manager)

    if command == "probe":
        return explore(settings, manager, args[1:])

    server = make_server(App(settings.oauth, manager, auth.PendingSignIns(), settings.db_path),
                         settings.listen_host, settings.listen_port)
    logging.getLogger("evdash").info("listening on %s:%s", settings.listen_host,
                                     settings.listen_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
