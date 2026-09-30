#!/usr/bin/env python3
"""Remove the systemd user timer, leaving local config, state, and logs."""

import os
import subprocess
import sys

from warmup import install_dir, unit_dir


def main():
    if os.geteuid() == 0:
        print("Run as your desktop user, without sudo", file=sys.stderr)
        return 1
    if not (unit_dir() / "claude-timer.timer").exists() and not (unit_dir() / "claude-timer.service").exists():
        print("claude-timer is not installed")
        return 0
    subprocess.run(["systemctl", "--user", "disable", "--now", "claude-timer.timer"], check=True)
    subprocess.run(["systemctl", "--user", "stop", "claude-timer.service"], check=True)
    for name in ("claude-timer.timer", "claude-timer.service"):
        (unit_dir() / name).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print("Removed claude-timer user timer")
    print(f"Local config, state, and logs remain in {install_dir()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
