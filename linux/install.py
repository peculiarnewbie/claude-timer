#!/usr/bin/env python3
"""Install the Claude window scheduler as a systemd user timer."""

import argparse
import getpass
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys

from warmup import (MODEL, Management, install_dir, management_key, proxy_uri, t3_key,
                    unit_dir, valid_key, write_json, write_private)


def unit_argument(value):
    """Quote a literal systemd ExecStart argument, including its specifiers."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def unit_files(directory, python, interval, proxy_service):
    dependency = f"Wants={proxy_service}\nAfter={proxy_service}\n" if proxy_service else ""
    service = (
        "[Unit]\nDescription=Start due Claude five-hour windows\n" + dependency
        + "\n[Service]\nType=oneshot\nUMask=0077\n"
        + f"ExecStart={unit_argument(python)} {unit_argument(directory / 'warmup.py')}\n"
        + "TimeoutStartSec=5min\n"
    )
    # A calendar timer catches up after suspend and downtime; Persistent applies to OnCalendar.
    # Group the day's tick times by their minute lists (at most 24 expressions).
    # Unlike a monotonic timer, wall-clock ticks resume immediately after suspend.
    minutes_by_hour = {}
    for minute in range(0, 1440, interval):
        hour, minute = divmod(minute, 60)
        minutes_by_hour.setdefault(hour, []).append(minute)
    hours_by_minutes = {}
    for hour, minutes in minutes_by_hour.items():
        hours_by_minutes.setdefault(tuple(minutes), []).append(hour)
    calendars = ""
    for minutes, hours in hours_by_minutes.items():
        hour_field = ",".join(f"{hour:02d}" for hour in hours)
        minute_field = ",".join(f"{minute:02d}" for minute in minutes)
        calendars += f"OnCalendar=*-*-* {hour_field}:{minute_field}:00\n"
    timer = (
        "[Unit]\nDescription=Check Claude window reset times\n\n[Timer]\n"
        + calendars
        + "OnActiveSec=1min\nPersistent=true\nAccuracySec=30s\n\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def configure_key(args, config):
    if args.management_key_file:
        config["managementKeyFile"] = str(args.management_key_file.expanduser().resolve())
    elif args.management_key_source == "auto" and (Path.home() / ".cli-proxy-api/management-token").is_file():
        config["managementKeyFile"] = str(Path.home() / ".cli-proxy-api/management-token")
    elif args.management_key_source != "prompt" and args.t3_settings_path.is_file():
        try:
            source, _ = t3_key(args.t3_settings_path, config["proxyUri"])
            config["t3SettingsPath"] = str(args.t3_settings_path.resolve())
            config["t3SourceId"] = source
        except ValueError:
            if args.management_key_source == "t3":
                raise
    elif args.management_key_source == "t3":
        raise ValueError("T3 settings were not found")
    if not config.get("managementKeyFile") and not config.get("t3SettingsPath"):
        if not sys.stdin.isatty():
            raise ValueError("No plaintext key found; pass --management-key-file")
        key = getpass.getpass("CLIProxyAPI management key: ").strip()
        if not valid_key(key):
            raise ValueError("Management key cannot be empty or masked")
        config["managementKey"] = key
    return management_key(config)


def install(args):
    if os.geteuid() == 0:
        raise ValueError("Run as your desktop user, without sudo")
    if not shutil.which("systemctl"):
        raise ValueError("systemd user services are required")
    subprocess.run(["systemctl", "--user", "show-environment"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    uri = proxy_uri(args.proxy_uri)
    if args.proxy_service and not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", args.proxy_service):
        raise ValueError("Proxy service must be a systemd service name")
    config = {"proxyUri": uri, "model": MODEL}
    if args.proxy_service:
        result = subprocess.run(["systemctl", "--user", "show", args.proxy_service, "-p", "LoadState", "--value"],
                                capture_output=True, text=True, check=True, timeout=15)
        if result.stdout.strip() != "loaded":
            raise ValueError("Configured proxy service is not loaded")
        config["proxyService"] = args.proxy_service
    key = configure_key(args, config)
    files = Management(uri, key).call("auth-files")["files"]
    available = {str(f.get("email", "")).lower(): f for f in files if f.get("provider") == "claude"}
    emails = sorted(email for email, f in available.items() if email and not f.get("disabled")) if args.all_accounts else [email.strip().lower() for email in args.account_emails]
    if not emails or len(set(emails)) != len(emails):
        raise ValueError("Select at least one Claude account; account emails must be unique")
    names = [name.strip() for name in args.account_names] if args.account_names else emails
    if len(names) != len(emails) or any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("Account names must be nonempty, unique, and match the number of emails")
    for email in emails:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError(f"Invalid account email: {email}")
        auth = available.get(email)
        if not auth or not auth.get("auth_index") or auth.get("disabled"):
            raise ValueError(f"Claude account is not authenticated and enabled: {email}")
        if auth.get("status") not in (None, "", "ready", "active"):
            raise ValueError(f"Claude account is not ready: {email}")
    config["accounts"] = [{"name": name.strip(), "email": email} for name, email in zip(names, emails)]
    directory = install_dir().resolve()
    units = unit_dir().resolve()
    if any(c in str(directory) + sys.executable for c in "\r\n\0"):
        raise ValueError("Install paths cannot contain line breaks")
    service, timer = unit_files(directory, Path(sys.executable).absolute(), args.interval_minutes, args.proxy_service)
    # Validate the calendar expression before changing an existing installation.
    expressions = [line.split("=", 1)[1] for line in timer.splitlines() if line.startswith("OnCalendar=")]
    subprocess.run(["systemd-analyze", "calendar", *expressions], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    if args.enable_linger:
        user = pwd.getpwuid(os.getuid()).pw_name
        result = subprocess.run(["loginctl", "show-user", user, "-p", "Linger", "--value"],
                                capture_output=True, text=True, check=True, timeout=15)
        if result.stdout.strip() != "yes":
            subprocess.run(["loginctl", "enable-linger", user], check=True, timeout=30)
    subprocess.run(["systemctl", "--user", "stop", "claude-timer.timer", "claude-timer.service"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    os.umask(0o077)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    units.mkdir(parents=True, exist_ok=True)
    write_private(directory / "warmup.py", Path(__file__).with_name("warmup.py").read_text())
    write_json(directory / "warmup-config.json", config)
    write_private(units / "claude-timer.service", service)
    write_private(units / "claude-timer.timer", timer)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
    subprocess.run(["systemctl", "--user", "enable", "--now", "claude-timer.timer"], check=True, timeout=30)
    print(f"Installed claude-timer ({args.interval_minutes}-minute ticks) for {len(emails)} accounts")
    print(f"Local config: {directory / 'warmup-config.json'}")
    print(f"Local log: {directory / 'warmup.log'}")
    if args.enable_linger:
        print("Starts at boot with user lingering enabled")
    print(f"Inspect with: python3 {directory / 'warmup.py'} --dry-run")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    accounts = parser.add_mutually_exclusive_group(required=True)
    accounts.add_argument("--account-emails", nargs="+")
    accounts.add_argument("--all-accounts", action="store_true", help="Select all currently enabled Claude accounts")
    parser.add_argument("--account-names", nargs="+")
    parser.add_argument("--proxy-uri", default="http://127.0.0.1:8317")
    parser.add_argument("--proxy-service", help="Existing systemd user service to start when the proxy is down")
    parser.add_argument("--management-key-file", type=Path)
    parser.add_argument("--management-key-source", choices=("auto", "t3", "prompt"), default="auto")
    parser.add_argument("--t3-settings-path", type=Path, default=Path.home() / ".t3/userdata/settings.json")
    parser.add_argument("--interval-minutes", type=int, choices=range(1, 1441), metavar="1..1440", default=10)
    parser.add_argument("--enable-linger", action="store_true", help="Start the user service manager at boot, even before login")
    args = parser.parse_args()
    try:
        install(args)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
