"""Command line: ``python -m evdash serve | status | refresh``."""
from __future__ import annotations

import logging
import sys

from . import auth, config, redact
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


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""
    if command not in ("serve", "status", "refresh"):
        print("usage: python -m evdash serve | status | refresh", file=sys.stderr)
        return 2
    try:
        settings = config.load()
    except config.ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2
    redact.install(known=(settings.oauth.client_id, settings.oauth.client_secret))
    store = auth.TokenStore(settings.token_file)
    manager = auth.TokenManager(settings.oauth, store)

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

    server = make_server(App(settings.oauth, manager, auth.PendingSignIns()),
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
