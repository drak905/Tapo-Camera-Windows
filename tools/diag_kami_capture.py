"""Capture the real Kami sign-in reply once, with all values hidden. FOR THE OWNER, NOT FOR AGENTS.

    python tools/diag_kami_capture.py --i-am-the-owner --email you@example.com --country DE

* Refuses to run without ``--i-am-the-owner``.
* The password is typed at a hidden prompt (``getpass``); it can not be given on the command line.
* Sends exactly ONE sign-in request. It does not list cameras, never retries and changes nothing.
  A wrong password may start the account's own lockout, so type it carefully.
* Writes the HTTP status, header names and the JSON field names (values hidden) to
  ``F:\\kami_re\\real_signin_reply.json`` (``--out`` changes the path). The file stays on this PC.
* The vendor key must be installed first (TAPO_KAMI_APP_KEY or kami_app_key.txt in the app data folder).
"""
from __future__ import annotations

import argparse
import getpass
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from tapo_desktop.core.kami.app_key import load_app_key  # noqa: E402
from tapo_desktop.core.kami.capture import capture_login, write_capture  # noqa: E402
from tapo_desktop.core.kami.client import KamiClient  # noqa: E402
from tapo_desktop.core.kami.dtos import KamiError  # noqa: E402
from tapo_desktop.core.kami.regions import region_for  # noqa: E402

DEFAULT_OUT = r"F:\kami_re\real_signin_reply.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign in to Kami Home once and save the redacted reply shape.")
    parser.add_argument("--i-am-the-owner", action="store_true", help="confirm this is your own Kami account")
    parser.add_argument("--email", help="Kami account e-mail")
    parser.add_argument("--country", help="two-letter country code of the account, e.g. DE")
    parser.add_argument("--out", default=DEFAULT_OUT, help="where to write the redacted reply")
    args = parser.parse_args(argv)

    if not args.i_am_the_owner:
        print("Refusing to run: this tool signs in to a real Kami account. Add --i-am-the-owner if the account "
              "is yours.", file=sys.stderr)
        return 2
    if not args.email or not args.country:
        print("--email and --country are required.", file=sys.stderr)
        return 2
    try:
        region = region_for(args.country)
    except ValueError:
        print("Unknown country code.", file=sys.stderr)
        return 2
    key = load_app_key()
    if not key:
        print("The Kami password key is not installed (TAPO_KAMI_APP_KEY or kami_app_key.txt).", file=sys.stderr)
        return 2
    password = getpass.getpass("Kami password (hidden): ")
    try:
        doc = capture_login(KamiClient(region, args.country, app_key=key), args.email, password)
    except KamiError as exc:
        print(f"Failed before a reply arrived: {type(exc).__name__}", file=sys.stderr)
        return 1
    path = write_capture(doc, args.out)
    print(f"Saved the redacted reply to {path} (HTTP {doc['status']}, {doc['body_kind']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
