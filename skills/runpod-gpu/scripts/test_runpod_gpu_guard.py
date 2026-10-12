"""有料Podや本物のsystemdを操作せず、料金・二重作成・削除対象の制御を検証する。"""

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import runpod_gpu_guard as guard


class FakeRuntime:
    """CLIとsystemdの境界だけを置き換える。未定義のコマンドは実行せず失敗させる。"""

    def __init__(self):
        self.pods = []
        self.list_payload = None
        self.quote_prices = [0.20]
        self.gpu_available = True
        self.outcomes = []
        self.creates = 0
        self.deletes = []
        self.lookup_fails = False
        self.delete_fails = False
        self.systemd_available = True
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append(command)
        assert kwargs["timeout"] > 0
        args = command[1:]

        def reply(payload, code=0):
            text = json.dumps(payload)
            return subprocess.CompletedProcess(
                command, code, text if not code else "", text if code else ""
            )

        if command[0] == guard.SYSTEMCTL:
            if "--property=Version" in args:
                return subprocess.CompletedProcess(
                    command,
                    0 if self.systemd_available else 1,
                    "255" if self.systemd_available else "",
                    "" if self.systemd_available else "systemd is unavailable",
                )
            text = {"is-active": "active", "is-enabled": "enabled"}.get(args[0], "")
            if args[0] == "show":
                text = (
                    "success"
                    if "--property=Result" in args
                    else "2099-01-01 00:00:00 UTC"
                )
            return subprocess.CompletedProcess(command, 0, text, "")
        assert command[0] == guard.RUNPODCTL, "unexpected external command"
        if args[:2] == ["pod", "list"]:
            if self.lookup_fails and self.creates:
                return reply({"code": "rate_limited", "error": "request limit"}, 1)
            return reply(self.pods if self.list_payload is None else self.list_payload)
        if args[:2] == ["gpu", "list"]:
            price = (
                self.quote_prices.pop(0)
                if len(self.quote_prices) > 1
                else self.quote_prices[0]
            )
            return reply(
                [
                    {
                        "gpuId": "test-gpu",
                        "available": self.gpu_available,
                        "memoryInGb": 16,
                        "communityPricePerHr": price,
                        "securePricePerHr": price,
                    }
                ]
            )
        if args[:2] == ["pod", "create"]:
            self.creates += 1
            outcome = self.outcomes.pop(0) if self.outcomes else "success"
            if outcome == "capacity":
                return reply(
                    {
                        "code": "graphql_error",
                        "error": "There are no longer any instances available",
                    },
                    1,
                )
            if outcome == "auth":
                return reply(
                    {"code": "unauthorized", "error": "invalid credentials"}, 1
                )
            pod = {
                "id": f"test-{self.creates}",
                "name": args[args.index("--name") + 1],
                "desiredStatus": "RUNNING",
                "costPerHr": 0.50 if outcome == "expensive" else 0.20,
                "containerDiskInGb": int(
                    args[args.index("--container-disk-in-gb") + 1]
                ),
                "volumeInGb": int(args[args.index("--volume-in-gb") + 1]),
            }
            self.pods.append(pod)
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if outcome == "wait_timeout":
                return reply(
                    {"code": "wait_timeout", "error": "SSH not ready", "id": pod["id"]},
                    1,
                )
            return reply({"unexpected": "schema"} if outcome == "unknown" else pod)
        if args[:2] == ["pod", "stop"]:
            return reply({})
        if args[:2] == ["pod", "delete"]:
            if self.delete_fails:
                return reply({"code": "rate_limited", "error": "request limit"}, 1)
            self.deletes.append(args[2])
            self.pods = [pod for pod in self.pods if pod["id"] != args[2]]
            return reply({})
        raise AssertionError(f"unexpected CLI command: {args}")


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.units = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.runtime = FakeRuntime()
        self.context.enter_context(patch.object(guard, "SYSTEMD_DIR", self.units))
        self.context.enter_context(patch.object(guard.sys, "platform", "linux"))
        self.context.enter_context(
            patch.object(guard.os, "geteuid", return_value=0, create=True)
        )
        self.context.enter_context(
            patch.object(
                guard.subprocess,
                "run",
                side_effect=lambda command, **kw: self.runtime.run(command, **kw),
            )
        )
        self.sleep = self.context.enter_context(patch.object(guard.time, "sleep"))

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = guard.main(list(args))
        # json.loadsは説明文や2つ目のJSONが混ざっていれば失敗する。
        return status, json.loads(stdout.getvalue()), stderr.getvalue()

    def create(self, *args):
        return self.invoke(
            "create",
            "--gpu-id",
            "test-gpu",
            "--image",
            "test-image",
            "--max-hourly-usd",
            "0.25",
            *args,
        )

    def test_confirmed_creation_rejects_unsupported_environment(self):
        for platform, uid, systemd in (
            ("win32", 0, True),
            ("linux", 1000, True),
            ("linux", 0, False),
        ):
            with (
                self.subTest(platform=platform, uid=uid, systemd=systemd),
                patch.object(guard.sys, "platform", platform),
                patch.object(guard.os, "geteuid", return_value=uid),
            ):
                self.runtime.systemd_available = systemd
                status, result, _ = self.create("--confirm")
                self.assertEqual(status, 1)
                self.assertEqual(result["code"], "unsupported_environment")
        self.assertFalse(any(call[0] == guard.RUNPODCTL for call in self.runtime.calls))
        self.assertEqual(list(self.units.iterdir()), [])
        self.assertEqual(self.sleep.call_count, 0)

    def test_read_only_commands_do_not_require_linux_or_root(self):
        with (
            patch.object(guard.sys, "platform", "win32"),
            patch.object(guard.os, "geteuid", return_value=1000),
        ):
            self.assertEqual(self.invoke("preflight")[0], 0)
            status, result, _ = self.create()
        self.assertEqual(status, 0)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.runtime.creates, 0)
        self.assertFalse(any(call[0] == guard.SYSTEMCTL for call in self.runtime.calls))
        self.assertEqual(list(self.units.iterdir()), [])

    def test_unknown_json_is_not_an_empty_pod_list(self):
        for payload in (
            {"unexpected": []},
            {"pods": None},
            {"data": [], "error": "failed"},
            ["unexpected"],
            [{}],
        ):
            with self.subTest(payload=payload):
                self.runtime.list_payload = payload
                status, result, _ = self.create("--confirm")
                self.assertEqual(status, 1)
                self.assertEqual(result["code"], "unknown_json_format")
        self.assertEqual(self.runtime.creates, 0)
        self.assertEqual(list(self.units.iterdir()), [])

    def test_storage_is_explicit_and_included_in_the_limit(self):
        _, default, _ = self.create()
        self.assertEqual(default["pricing"]["volume_gb"], 0)
        self.assertTrue(default["dry_run"])
        _, with_volume, _ = self.create("--volume-in-gb", "100")
        self.assertGreater(
            with_volume["pricing"]["total_usd_per_hour_estimate"],
            default["pricing"]["total_usd_per_hour_estimate"],
        )
        status, result, _ = self.create("--max-hourly-usd", "0.20")
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "price_limit")
        self.assertEqual(self.runtime.creates, 0)

    def test_multiple_pods_require_permission_and_warn(self):
        existing = {"id": "existing", "name": "another-job", "desiredStatus": "RUNNING"}
        self.runtime.pods = [existing]
        _, refused, _ = self.create("--confirm")
        self.assertEqual(refused["code"], "active_pods")
        self.assertEqual(self.runtime.creates, 0)
        status, result, stderr = self.create("--confirm", "--allow-multiple")
        self.assertEqual(status, 0)
        self.assertTrue(result["warnings"])
        self.assertIn("warning:", stderr)
        self.assertIn(existing, self.runtime.pods)
        self.assertEqual(len(self.runtime.pods), 2)

    def test_price_is_refreshed_without_raising_the_limit(self):
        self.runtime.quote_prices = [0.30, 0.20]
        status, result, _ = self.create("--confirm")
        self.assertEqual(status, 0)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["max_hourly_usd"], 0.25)
        self.assertEqual(self.runtime.creates, 1)
        self.assertEqual(self.sleep.call_count, 1)

    def test_inventory_hint_does_not_prevent_guarded_creation(self):
        self.runtime.gpu_available = False
        _, quote, _ = self.create()
        self.assertTrue(quote["dry_run"])
        self.assertFalse(quote["available"])
        self.assertEqual(self.runtime.creates, 0)
        status, result, stderr = self.create("--confirm")
        self.assertEqual(status, 0)
        self.assertEqual(self.runtime.creates, 1)
        self.assertFalse(result["available"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("warning:", stderr)
        self.assertEqual(len(list(self.units.glob("*.timer"))), 1)

    def test_unavailable_inventory_hint_does_not_bypass_price_limit(self):
        self.runtime.gpu_available = False
        self.runtime.quote_prices = [0.30]
        _, result, _ = self.create("--confirm")
        self.assertEqual(result["code"], "price_limit")
        self.assertEqual(self.runtime.creates, 0)
        self.assertEqual(list(self.units.iterdir()), [])

    def test_capacity_retry_is_bounded_and_keeps_one_deadline(self):
        self.runtime.outcomes = ["capacity"] * 3
        status, result, _ = self.create("--confirm")
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "gpu_unavailable")
        self.assertEqual(self.runtime.creates, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(len(list(self.units.glob("*.timer"))), 1)
        names = {
            call[call.index("--name") + 1]
            for call in self.runtime.calls
            if call[1:3] == ["pod", "create"]
        }
        self.assertEqual(len(names), 1)

    def test_authentication_error_is_not_retried(self):
        self.runtime.outcomes = ["auth"]
        _, result, _ = self.create("--confirm")
        self.assertEqual(result["code"], "unauthorized")
        self.assertEqual(self.runtime.creates, 1)
        self.assertEqual(self.sleep.call_count, 0)

    def test_created_or_uncertain_pods_are_not_created_again(self):
        for outcome in ("timeout", "wait_timeout", "unknown"):
            for lookup_fails in (False, True):
                with self.subTest(outcome=outcome, lookup_fails=lookup_fails):
                    self.runtime = FakeRuntime()
                    self.runtime.outcomes = [outcome]
                    self.runtime.lookup_fails = lookup_fails
                    _, result, _ = self.create("--confirm")
                    self.assertFalse(result["ok"])
                    self.assertIn("watchdog_timer", result)
                    self.assertEqual(self.runtime.creates, 1)
                    self.assertEqual(self.sleep.call_count, 0)
                    if lookup_fails:
                        self.assertEqual(result["code"], "creation_uncertain")
                    else:
                        self.assertTrue(result["resource_created"])

    def test_rejected_allocation_is_deleted_before_retry(self):
        unrelated = {
            "id": "unrelated",
            "name": "another-job",
            "desiredStatus": "EXITED",
        }
        self.runtime.pods = [unrelated]
        self.runtime.outcomes = ["expensive", "success"]
        status, result, _ = self.create("--confirm")
        self.assertEqual(status, 0)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(self.runtime.deletes, ["test-1"])
        self.assertIn(unrelated, self.runtime.pods)
        self.assertEqual(len(list(self.units.glob("*.timer"))), 1)

    def test_unconfirmed_deletion_prevents_another_purchase(self):
        self.runtime.outcomes = ["expensive"]
        self.runtime.delete_fails = True
        _, result, _ = self.create("--confirm")
        self.assertEqual(result["code"], "cleanup_incomplete")
        self.assertEqual(self.runtime.creates, 1)
        self.assertEqual(self.sleep.call_count, 0)

    def test_invalid_limits_fail_before_any_api_call(self):
        for args in (
            ("--max-hourly-usd", "nan"),
            ("--min-vram-gb", "inf"),
            ("--volume-in-gb", "-1"),
            ("--wait", "--wait-timeout", "0s"),
        ):
            with self.subTest(args=args):
                status, result, _ = self.create(*args)
                self.assertEqual(status, 2)
                self.assertEqual(result["code"], "usage_error")
        self.assertEqual(self.runtime.calls, [])

    def test_cleanup_does_not_claim_success_on_unknown_json(self):
        self.runtime.list_payload = {"unexpected": []}
        status, result, _ = self.invoke("cleanup", "test-id", "--confirm")
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "unknown_json_format")


class TimeoutTests(unittest.TestCase):
    def test_cli_is_actually_terminated_at_its_timeout(self):
        # 実行するのは待機だけのローカルPythonプロセスであり、RunPodには接続しない。
        with (
            patch.object(guard, "RUNPODCTL", guard.PYTHON),
            self.assertRaises(guard.GuardError) as caught,
        ):
            guard.run_ctl("-c", "import time; time.sleep(3)", timeout=0.05)
        self.assertEqual(caught.exception.code, "cli_timeout")


if __name__ == "__main__":
    unittest.main()
