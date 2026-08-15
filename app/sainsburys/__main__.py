"""CLI for the Sainsbury's shopping session.

    python -m app.sainsburys status
    python -m app.sainsburys login
    python -m app.sainsburys basket
    python -m app.sainsburys logout

``login`` is the only one that can need a person: Sainsbury's emails a six-digit
code the first time an account signs in from somewhere new. If a mailbox is
configured the code is read from it; otherwise this prompts for it. Either way it
is a one-off — the refresh token it comes back with is what every later session
is built from, so this should not need running again unless the token is revoked.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.sainsburys.auth import AuthError, AuthState
from app.sainsburys.session import SainsburysSession


def _status(session: SainsburysSession) -> int:
    """What is stored, and whether it is worth trying.

    Reports rather than probes. There is no endpoint that reliably answers "am I
    signed in" (see app/sainsburys/session.py), so claiming a live/dead verdict
    here would be inventing certainty the site does not offer. What can be said
    honestly is what the ladder itself goes on.
    """
    tokens = session.tokens
    usable = session.looks_authenticated()
    print(f"state:       {session.state.value}")
    print(f"cookie:      {'signed in' if session.has_auth_cookie() else 'none'}")
    print(f"access:      {'live' if tokens and not tokens.expired else 'expired or none'}")
    print(f"refresh:     {'stored' if tokens and tokens.refresh_token else 'none'}")
    if tokens and tokens.expires_at:
        print(f"expires:     {tokens.expires_at.isoformat()}")
    print(f"jar:         {session.jar_path}")
    if not usable and tokens and tokens.refresh_token:
        print("\nThe shopping session needs re-minting; the stored refresh token can do")
        print("that without a code. Any request will do it, or run `login`.")
    return 0 if usable else 1


def _login(session: SainsburysSession) -> int:
    # Each step is announced before it blocks. Signing in is several seconds of
    # network with nothing to show for it, and a CLI that goes quiet for that
    # long is indistinguishable from one that has hung.
    say("Signing in to Sainsbury's…")
    # Never waits on the OTP mailbox: whoever ran this can read their own email
    # faster than a forwarding rule can, and the wait is invisible while it
    # happens. Unattended callers (the API) still use the mailbox.
    state = session.ensure_authenticated(allow_mailbox=False)

    if state == AuthState.AWAITING_OTP:
        say("Sainsbury's has emailed a six-digit code to the account address.")
        try:
            code = input("Code: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAbandoned; no code entered.", file=sys.stderr)
            return 1
        if not code:
            print("No code entered.", file=sys.stderr)
            return 1
        say("Submitting the code…")
        state = session.submit_otp(code)

    if state == AuthState.READY:
        say("Signed in. Refresh token stored; this should not need repeating.")
        return 0
    print(f"Not signed in ({state.value}).", file=sys.stderr)
    return 1


def say(message: str) -> None:
    """Print and flush.

    The flush matters: stdout is block-buffered whenever this is piped into
    anything, so progress written without it arrives all at once at the end —
    exactly when it has stopped being progress.
    """
    print(message, flush=True)


def _basket(session: SainsburysSession) -> int:
    """Read the trolley back. The quickest proof that a session really shops."""
    from app.sainsburys.client import SainsburysClient

    client = SainsburysClient(session)
    print(json.dumps(client.basket(), indent=2)[:4000])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.sainsburys")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="whether the stored session still shops")
    sub.add_parser("login", help="sign in, asking for the emailed code if needed")
    sub.add_parser("basket", help="print the live trolley")
    sub.add_parser("logout", help="forget the stored session and tokens")
    args = parser.parse_args(argv)

    session = SainsburysSession()
    try:
        if args.command == "status":
            return _status(session)
        if args.command == "login":
            return _login(session)
        if args.command == "basket":
            return _basket(session)
        if args.command == "logout":
            session.forget()
            print("Forgotten.")
            return 0
    except AuthError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
