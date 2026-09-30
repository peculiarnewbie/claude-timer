#!/usr/bin/env python3
"""Start due Claude windows through CLIProxyAPI using only the Python stdlib."""

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MODEL = "claude-haiku-4-5-20251001"
UTC = timezone.utc


def install_dir():
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "claude-timer"


def unit_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def write_private(path, text):
    """Replace atomically, with mode 0600 even when overwriting an existing file."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    write_private(path, json.dumps(value, indent=2) + "\n")


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Reset/state timestamp is missing its timezone")
    return result.astimezone(UTC)


def local_time(value):
    return value.astimezone().isoformat(timespec="seconds")


def proxy_uri(value):
    uri = urlsplit(value)
    if (uri.scheme != "http" or uri.hostname not in ("localhost", "127.0.0.1", "::1")
            or uri.username is not None or uri.password is not None
            or uri.path not in ("", "/") or uri.query or uri.fragment):
        raise ValueError("Proxy URI must be a local HTTP address without credentials or a path")
    _ = uri.port  # Validate the port before making any requests.
    return value.rstrip("/")


def valid_key(value):
    return (isinstance(value, str) and bool(value.strip()) and value.isascii()
            and not any(c in value for c in "\r\n\0")
            and not all(c == "*" for c in value))


def t3_key(path, uri, source_id=None):
    sources = read_json(path).get("usageLimitSources", {})
    matches = []
    for name, source in sources.items():
        if source_id and name != source_id:
            continue
        source_uri = source.get("url") or source.get("proxyUri") or source.get("baseUrl")
        if not source_uri and name == "cliproxy_local":
            source_uri = "http://127.0.0.1:8317"
        if source_uri and source_uri.rstrip("/") == uri and valid_key(source.get("managementKey")):
            matches.append((name, source["managementKey"].strip()))
    if len(matches) != 1:
        raise ValueError("No unique plaintext T3 management key for this proxy; use --management-key-file")
    return matches[0]


def management_key(config):
    if config.get("managementKeyFile"):
        key = Path(config["managementKeyFile"]).read_text().strip()
    elif config.get("t3SettingsPath"):
        _, key = t3_key(config["t3SettingsPath"], config["proxyUri"], config.get("t3SourceId"))
    else:
        key = config.get("managementKey")
    if not valid_key(key):
        raise ValueError("Plaintext management key is missing or masked")
    return key.strip()


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Management:
    def __init__(self, uri, key):
        self.uri = proxy_uri(uri)
        self.key = key
        # Local management traffic must not inherit an HTTP proxy or follow redirects.
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def call(self, path, body=None):
        request = Request(
            self.uri + "/v0/management/" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Management-Key": self.key, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Management {path} returned HTTP {exc.code}") from None
        except URLError:
            raise RuntimeError("Cannot reach CLIProxyAPI management API") from None
        if not isinstance(result, dict):
            raise ValueError("Unexpected management response")
        return result

    def upstream(self, auth_index, method, url, headers, data=None):
        body = {"auth_index": auth_index, "method": method, "url": url, "header": headers}
        if data is not None:
            body["data"] = json.dumps(data, separators=(",", ":"))
        response = self.call("api-call", body)
        if response.get("status_code") != 200:
            raise RuntimeError(f"Upstream {method} returned HTTP {response.get('status_code')}")
        result = json.loads(response["body"])
        if not isinstance(result, dict):
            raise ValueError("Unexpected upstream response")
        return result

    def quota(self, auth_index):
        result = self.upstream(auth_index, "GET", "https://api.anthropic.com/api/oauth/usage", {
            "Authorization": "Bearer $TOKEN$", "anthropic-beta": "oauth-2025-04-20",
        })
        # Missing fields must not accidentally be treated as an expired window.
        for name in ("five_hour", "seven_day"):
            if name not in result:
                raise ValueError(f"Quota response is missing {name}")
            window = result[name]
            if window is not None:
                if not isinstance(window, dict):
                    raise ValueError(f"Unexpected {name} quota")
                utilization = window.get("utilization")
                if (not isinstance(utilization, (int, float)) or isinstance(utilization, bool)
                        or not 0 <= utilization <= 100 or "resets_at" not in window):
                    raise ValueError(f"Unexpected {name} quota fields")
                if window["resets_at"]:
                    timestamp(window["resets_at"])
                elif utilization > 0:
                    raise ValueError(f"{name} quota has usage without a reset time")
        return result

    def warmup(self, auth_index, model):
        result = self.upstream(auth_index, "POST", "https://api.anthropic.com/v1/messages", {
            "Authorization": "Bearer $TOKEN$",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "claude-cli/2.1.280 (external, cli)",
        }, {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "Reply OK."}]})
        if result.get("type") != "message":
            raise ValueError("Haiku returned an unexpected response")


def ensure_proxy(config, dry_run, log):
    uri = urlsplit(proxy_uri(config["proxyUri"]))

    def listening():
        try:
            with socket.create_connection((uri.hostname, uri.port or 80), timeout=1):
                return True
        except OSError:
            return False

    if listening():
        return
    if dry_run:
        raise RuntimeError("Proxy is not running")
    if config.get("proxyService"):
        subprocess.run(["systemctl", "--user", "start", config["proxyService"]], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    else:
        raise RuntimeError("Proxy is not running; configure --proxy-service")
    for _ in range(20):
        if listening():
            log("Started CLIProxyAPI")
            return
        time.sleep(1)
    raise RuntimeError("Proxy did not start")


def run(config_path, dry_run=False):
    base = Path(config_path).parent
    state_path = base / "warmup-state.json"
    os.umask(0o077)

    def log(message):
        line = f"{datetime.now(UTC).isoformat()} {message}"
        print(line, flush=True)
        if not dry_run:
            with (base / "warmup.log").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def save():
        if not dry_run:
            write_json(state_path, state)

    with (base / "warmup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            config = read_json(config_path)
            accounts = config["accounts"]
            if not accounts:
                raise ValueError("No Claude accounts configured")
            state = read_json(state_path) if state_path.exists() else {}
            if not isinstance(state, dict):
                raise ValueError("Unexpected scheduler state")
            client = None
            files = None
            failed = False
            for account in accounts:
                name, email = account["name"], account["email"]
                entry = state.setdefault(email, {})
                try:
                    now = datetime.now(UTC)
                    if entry.get("nextCheckUtc") and now < timestamp(entry["nextCheckUtc"]):
                        if dry_run:
                            log(f"{name}: next check {local_time(timestamp(entry['nextCheckUtc']))}")
                        continue
                    if client is None:
                        ensure_proxy(config, dry_run, log)
                        client = Management(config["proxyUri"], management_key(config))
                        files = client.call("auth-files")["files"]
                    auth = next((f for f in files if f.get("provider") == "claude"
                                 and str(f.get("email", "")).lower() == email), None)
                    if auth is None or not auth.get("auth_index"):
                        raise ValueError("Claude account is missing")
                    if auth.get("disabled") or auth.get("status") not in (None, "", "ready", "active"):
                        raise ValueError("Claude account is disabled or not ready")
                    quota = client.quota(auth["auth_index"])
                    now = datetime.now(UTC)
                    weekly = quota.get("seven_day")
                    window = quota.get("five_hour")
                    next_check = None
                    reason = None
                    if weekly and weekly["utilization"] >= 100:
                        reset = timestamp(weekly["resets_at"]) if weekly["resets_at"] else now
                        next_check = max(reset + timedelta(minutes=2), now + timedelta(hours=1))
                        reason = "weekly quota exhausted"
                    elif window and window["resets_at"] and timestamp(window["resets_at"]) > now:
                        next_check = timestamp(window["resets_at"]) + timedelta(minutes=2)
                        reason = "window active"
                    elif entry.get("lastWarmupUtc") and now < timestamp(entry["lastWarmupUtc"]) + timedelta(hours=5):
                        next_check = timestamp(entry["lastWarmupUtc"]) + timedelta(hours=5)
                        reason = "recent warmup"
                    if next_check:
                        entry["nextCheckUtc"] = next_check.isoformat()
                        save()
                        log(f"{name}: {reason}; next check {local_time(next_check)}")
                        continue
                    if dry_run:
                        log(f"{name}: would send Haiku warmup")
                        continue
                    # Persist before sending: an uncertain response must not cause repeated messages.
                    entry["lastWarmupUtc"] = now.isoformat()
                    entry["nextCheckUtc"] = (now + timedelta(hours=5)).isoformat()
                    save()
                    client.warmup(auth["auth_index"], config.get("model", MODEL))
                    log(f"{name}: Haiku warmup succeeded")
                    try:
                        after = client.quota(auth["auth_index"]).get("five_hour")
                        if after and after["resets_at"]:
                            next_check = timestamp(after["resets_at"]) + timedelta(minutes=2)
                            if next_check > now + timedelta(hours=4):
                                entry["nextCheckUtc"] = next_check.isoformat()
                                save()
                    except Exception as exc:
                        log(f"{name}: post-warmup quota check failed: {exc}")
                except Exception as exc:
                    failed = True
                    log(f"{name}: {exc}")
                    if not entry.get("nextCheckUtc") or timestamp(entry["nextCheckUtc"]) <= datetime.now(UTC):
                        entry["nextCheckUtc"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
                        save()
            return int(failed)
        except Exception as exc:
            log(f"Scheduler error: {exc}")
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().with_name("warmup-config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Inspect quota without sending messages or writing state/logs")
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error("Missing local config; run linux/install.py")
    return run(args.config, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
