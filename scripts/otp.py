"""Get the current one-time code for a MuleTrace account.

The second factor normally comes from an authenticator app on your phone. This
is for the cases where that is not practical: signing in on a machine with no
phone to hand, scripted testing, or a demo where you would rather not hold a
phone up to a projector.

    python scripts/otp.py                    current code and seconds remaining
    python scripts/otp.py --watch            keep printing as it rolls over
    python scripts/otp.py --enrol            secret + otpauth URI for this account
    python scripts/otp.py --new-secret       generate a fresh secret (for deploys)
    python scripts/otp.py --reset-enrolment  make /login show the QR again
    python scripts/otp.py --user someone     pick a different account

Anyone who can run this can sign in, because it reads the shared secret from
data/users.json. That is the same trust level as the file itself - keep both off
shared machines, and do not use this against a real deployment.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import auth  # noqa: E402

STEP = 30


def remaining() -> int:
    return STEP - int(time.time()) % STEP


def main() -> int:
    ap = argparse.ArgumentParser(description="Current TOTP code for a MuleTrace account.")
    ap.add_argument("--user", default="investigator")
    ap.add_argument("--watch", action="store_true", help="keep printing as codes roll over")
    ap.add_argument("--enrol", action="store_true", help="show the secret and otpauth URI")
    ap.add_argument("--reset-enrolment", action="store_true",
                    help="mark the account un-enrolled so /login shows the QR again")
    ap.add_argument("--new-secret", action="store_true",
                    help="generate a fresh secret to pin in a deployment")
    args = ap.parse_args()

    if args.new_secret:
        # Deliberately does not touch any existing account: this is for pinning
        # MULETRACE_TOTP_SECRET on a host with an ephemeral filesystem.
        secret = auth.new_totp_secret()
        print(f"MULETRACE_TOTP_SECRET={secret}")
        print()
        print(f"otpauth URI : {auth.provisioning_uri(args.user, secret)}")
        print("Add that to your authenticator, and set the variable on the host")
        print("alongside MULETRACE_USER and MULETRACE_PASSWORD.")
        return 0

    user = auth.get_user(args.user)
    if not user:
        print(f"No account '{args.user}'. Accounts live in {auth.USERS_FILE}.")
        print("Create one:")
        print(f"  python -c \"import sys;sys.path.insert(0,'backend');import auth;"
              f"auth.create_user('{args.user}','a-password')\"")
        return 1

    secret = user["totp_secret"]

    if args.reset_enrolment:
        users = auth._read_users()
        users[args.user]["mfa_enrolled"] = "0"
        auth._write_users(users)
        print(f"'{args.user}' marked un-enrolled.")
        print("The next sign-in will show the QR code again on /login.")
        return 0

    if args.enrol:
        print(f"account : {args.user}")
        print(f"secret  : {secret}")
        print(f"otpauth : {auth.provisioning_uri(args.user, secret)}")
        print("\nAdd the secret to any authenticator app (Google Authenticator,")
        print("Microsoft Authenticator, Authy, 1Password) as a time-based key.")
        return 0

    def show() -> None:
        code = auth.totp_at(secret, int(time.time() // STEP))
        left = remaining()
        bar = "#" * (left // 2) + "." * ((STEP - left) // 2)
        print(f"  {code}   valid {left:2d}s  [{bar}]", flush=True)

    if not args.watch:
        show()
        if remaining() <= 3:
            print("  (about to roll over - if it is rejected, take the next one)")
        return 0

    print(f"Codes for '{args.user}' — Ctrl+C to stop\n")
    last = None
    try:
        while True:
            counter = int(time.time() // STEP)
            if counter != last:
                show()
                last = counter
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
