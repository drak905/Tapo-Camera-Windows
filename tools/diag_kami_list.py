"""Read-only check of a Kami Home account: sign in once and list the cameras. FOR THE OWNER, NOT FOR AGENTS.

    python tools/diag_kami_list.py --i-am-the-owner --email you@example.com --country DE

* Refuses to run without ``--i-am-the-owner``.
* The password is typed at a hidden prompt (``getpass``); it can not be given on the command line or read from a file.
* Sends exactly ONE sign-in request and ONE device-list request. It never retries, never renames, never changes anything.
  A wrong password may start the account's own lockout rules (unknown), so type it carefully.
* Signing in on the PC may log the Kami phone app out once (not known). The vendor key must be installed first
  (TAPO_KAMI_APP_KEY or kami_app_key.txt in the app data folder).
* Prints a camera count and per camera: a short id (first 4 characters), the model code, online and shared flags.
  It prints no password, token, token secret, full uid, camera name, URL or camera password.

Agents must not run this against the real server.
"""
from __future__ import annotations

import argparse
import getpass
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from tapo_desktop.core.kami.app_key import load_app_key  # noqa: E402
from tapo_desktop.core.kami.client import KamiClient  # noqa: E402
from tapo_desktop.core.kami.dtos import KamiError  # noqa: E402
from tapo_desktop.core.kami.regions import countries, region_for  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign in to Kami Home once and list the cameras (read-only).")
    parser.add_argument("--i-am-the-owner", action="store_true", help="confirm this is your own Kami account")
    parser.add_argument("--email", help="Kami account e-mail")
    parser.add_argument("--country", help="two-letter country code of the account, e.g. DE")
    parser.add_argument("--list-countries", action="store_true", help="print the country codes and exit")
    args = parser.parse_args(argv)

    if args.list_countries:
        for code, name in countries():
            print(code, name)
        return 0
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
        print("Unknown country code. Use --list-countries.", file=sys.stderr)
        return 2
    key = load_app_key()
    if not key:
        print("The Kami password key is not installed (TAPO_KAMI_APP_KEY or kami_app_key.txt).", file=sys.stderr)
        return 2
    password = getpass.getpass("Kami password (hidden): ")
    client = KamiClient(region, args.country, app_key=key)
    try:
        session = client.login(args.email, password)
        devices = client.list_devices(session)
    except KamiError as exc:
        print(f"Failed: {type(exc).__name__} (code {getattr(exc, 'code', '-')})", file=sys.stderr)
        return 1
    print(f"Signed in (region {region}). Cameras: {len(devices)}")
    for dev in devices:
        print(f"  {dev.uid[:4]}...  model={dev.model or '-'}  online={dev.online}  shared={dev.shared}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
