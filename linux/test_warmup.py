"""Exercise the real scheduler against a local management HTTP server."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from install import unit_files
from warmup import MODEL, UTC, read_json, run, t3_key, write_json


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.config_path = self.base / "warmup-config.json"
        self.state_path = self.base / "warmup-state.json"
        self.requests = []
        self.warmup_status = 200
        self.quota_status = 200
        self.invalid_json = False
        self.expect_recorded_attempt = True
        self.accounts = [{"provider": "claude", "email": "first@example.com", "auth_index": "account-1", "status": "active"}]
        self.quota = {
            "five_hour": {"utilization": 0, "resets_at": None},
            "seven_day": {"utilization": 10, "resets_at": self.future(hours=24)},
        }
        scheduler = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                scheduler.requests.append(("GET", self.path, None))
                self.respond({"files": scheduler.accounts})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                scheduler.requests.append(("POST", self.path, body))
                scheduler.assertEqual(self.path, "/v0/management/api-call")
                scheduler.assertEqual(self.headers["X-Management-Key"], "test-key")
                scheduler.assertEqual(body["header"]["Authorization"], "Bearer $TOKEN$")
                if body["method"] == "GET":
                    scheduler.assertEqual(body["url"], "https://api.anthropic.com/api/oauth/usage")
                    data = "bad json" if scheduler.invalid_json else json.dumps(scheduler.quota)
                    self.respond({"status_code": scheduler.quota_status, "body": data})
                else:
                    scheduler.assertEqual(body["url"], "https://api.anthropic.com/v1/messages")
                    scheduler.assertEqual(json.loads(body["data"]), {
                        "model": MODEL, "max_tokens": 8,
                        "messages": [{"role": "user", "content": "Reply OK."}],
                    })
                    # Observe durable state while handling the actual HTTP request.
                    if scheduler.expect_recorded_attempt:
                        entry = read_json(scheduler.state_path)["first@example.com"]
                        scheduler.assertIn("lastWarmupUtc", entry)
                        scheduler.assertGreater(datetime.fromisoformat(entry["nextCheckUtc"]), datetime.now(UTC))
                    scheduler.quota["five_hour"] = {"utilization": 1, "resets_at": scheduler.future(hours=5)}
                    self.respond({"status_code": scheduler.warmup_status, "body": json.dumps({"type": "message"})})

            def respond(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        write_json(self.config_path, {
            "proxyUri": f"http://127.0.0.1:{self.server.server_port}",
            "managementKey": "test-key", "model": MODEL,
            "accounts": [{"name": "first", "email": "first@example.com"}],
        })

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def future(self, **kwargs):
        return (datetime.now(UTC) + timedelta(**kwargs)).isoformat()

    def execute(self, dry_run=False):
        output = io.StringIO()
        with redirect_stdout(output):
            result = run(self.config_path, dry_run)
        return result, output.getvalue()

    def sent_messages(self):
        return [body for _, _, body in self.requests if body and body["method"] == "POST"]

    def test_expired_window_warms_once_and_persists_before_sending(self):
        self.quota["five_hour"]["resets_at"] = self.future(minutes=-5)
        self.assertEqual(self.execute()[0], 0)
        self.assertEqual(len(self.sent_messages()), 1)
        self.assertEqual(self.sent_messages()[0]["auth_index"], "account-1")
        count = len(self.requests)
        self.assertEqual(self.execute()[0], 0)
        self.assertEqual(len(self.requests), count)
        entry = read_json(self.state_path)["first@example.com"]
        self.assertGreater(datetime.fromisoformat(entry["nextCheckUtc"]), datetime.now(UTC) + timedelta(hours=5))
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)

    def test_active_window_waits_even_with_less_than_one_minute_left(self):
        reset = self.future(seconds=30)
        self.quota["five_hour"] = {"utilization": 80, "resets_at": reset}
        self.assertEqual(self.execute()[0], 0)
        self.assertFalse(self.sent_messages())
        entry = read_json(self.state_path)["first@example.com"]
        self.assertEqual(datetime.fromisoformat(entry["nextCheckUtc"]), datetime.fromisoformat(reset) + timedelta(minutes=2))

    def test_weekly_exhaustion_waits_for_weekly_reset(self):
        reset = self.future(days=2)
        self.quota["seven_day"] = {"utilization": 100, "resets_at": reset}
        self.assertEqual(self.execute()[0], 0)
        self.assertFalse(self.sent_messages())
        self.assertEqual(datetime.fromisoformat(read_json(self.state_path)["first@example.com"]["nextCheckUtc"]),
                         datetime.fromisoformat(reset) + timedelta(minutes=2))

    def test_dry_run_queries_quota_without_sending_or_writing(self):
        write_json(self.state_path, {})
        before = self.state_path.read_bytes()
        result, output = self.execute(dry_run=True)
        self.assertEqual(result, 0)
        self.assertIn("would send Haiku warmup", output)
        self.assertTrue(self.requests)
        self.assertFalse(self.sent_messages())
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertFalse((self.base / "warmup.log").exists())

    def test_failed_warmup_keeps_five_hour_duplicate_guard(self):
        self.warmup_status = 504
        self.assertEqual(self.execute()[0], 1)
        entry = read_json(self.state_path)["first@example.com"]
        self.assertGreater(datetime.fromisoformat(entry["nextCheckUtc"]), datetime.now(UTC) + timedelta(hours=4))
        self.assertEqual(self.execute()[0], 0)
        self.assertEqual(len(self.sent_messages()), 1)

    def test_invalid_or_failed_quota_never_warms(self):
        for mode in ("missing", "json", "http", "reset"):
            with self.subTest(mode=mode):
                self.state_path.unlink(missing_ok=True)
                self.requests.clear()
                self.quota_status = 401 if mode == "http" else 200
                self.invalid_json = mode == "json"
                self.quota = {} if mode == "missing" else {
                    "five_hour": {"utilization": 1 if mode == "reset" else 0, "resets_at": None},
                    "seven_day": None,
                }
                self.assertEqual(self.execute()[0], 1)
                self.assertFalse(self.sent_messages())
                entry = read_json(self.state_path)["first@example.com"]
                self.assertNotIn("lastWarmupUtc", entry)

    def test_recent_attempt_blocks_warmup_when_saved_check_is_due(self):
        write_json(self.state_path, {"first@example.com": {"lastWarmupUtc": self.future(hours=-1)}})
        self.assertEqual(self.execute()[0], 0)
        self.assertFalse(self.sent_messages())

    def test_an_account_failure_does_not_block_another_account(self):
        config = read_json(self.config_path)
        config["accounts"].insert(0, {"name": "missing", "email": "missing@example.com"})
        write_json(self.config_path, config)
        self.assertEqual(self.execute()[0], 1)
        self.assertEqual(len(self.sent_messages()), 1)

    def test_process_lock_prevents_overlapping_requests(self):
        with (self.base / "warmup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("warmup.py")),
                                     "--config", str(self.config_path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.requests)

    def test_future_saved_check_works_even_when_proxy_is_down(self):
        config = read_json(self.config_path)
        config["proxyUri"] = "http://127.0.0.1:1"
        write_json(self.config_path, config)
        write_json(self.state_path, {"first@example.com": {"nextCheckUtc": self.future(hours=1)}})
        self.assertEqual(self.execute(dry_run=True)[0], 0)
        self.assertFalse(self.requests)

    def test_disabled_account_never_warms(self):
        self.accounts[0]["disabled"] = True
        self.assertEqual(self.execute()[0], 1)
        self.assertFalse(self.sent_messages())

    def test_t3_masks_are_rejected_and_modern_source_ids_are_supported(self):
        path = self.base / "settings.json"
        uri = "http://127.0.0.1:8317"
        write_json(path, {"usageLimitSources": {"cliproxy-127.0.0.1-8317": {"url": uri, "managementKey": "••••••"}}})
        with self.assertRaises(ValueError):
            t3_key(path, uri)
        write_json(path, {"usageLimitSources": {"cliproxy-127.0.0.1-8317": {"url": uri, "managementKey": "test-key"}}})
        self.assertEqual(t3_key(path, uri), ("cliproxy-127.0.0.1-8317", "test-key"))


class UnitTests(unittest.TestCase):
    def test_units_are_accepted_by_systemd_with_quoted_paths(self):
        for interval in (1, 7, 10, 90, 1440):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                service, timer = unit_files(Path('/tmp/timer with % and $ spaces'), Path(sys.executable), interval, "cliproxyapi.service")
                (directory / "claude-timer.service").write_text(service)
                (directory / "claude-timer.timer").write_text(timer)
                result = subprocess.run(["systemd-analyze", "--user", "verify", str(directory / "claude-timer.service"),
                                         str(directory / "claude-timer.timer")], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                expressions = [line.split("=", 1)[1] for line in timer.splitlines() if line.startswith("OnCalendar=")]
                result = subprocess.run(["systemd-analyze", "calendar", *expressions], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Persistent=true", timer)
                self.assertIn('%% and $$', service)


if __name__ == "__main__":
    unittest.main()
