#!/usr/bin/env python3
"""RunPodのGPU検証で、料金と停止漏れを抑えるための薄いCLIラッパー。"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

RUNPODCTL = os.environ.get("RUNPOD_GUARD_RUNPODCTL", "/usr/local/bin/runpodctl")
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_DIR = Path("/etc/systemd/system")
PYTHON = "/usr/bin/python3"
MAX_LIFETIME_MINUTES = 60
CLI_TIMEOUT_SECONDS = 30
CREATE_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10
# 月額からの換算は見積もりであり、RunPodの請求額を取得したものではない。
DISK_USD_PER_GB_MONTH = 0.10
STORAGE_MONTH_HOURS = 30 * 24


class GuardError(RuntimeError):
    """呼び出し元が理由と対象PodをJSONで受け取れるエラー。"""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def run_ctl(
    *args: str, timeout: float = CLI_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """CLIの終了を待つ時間を制限する。作成のタイムアウトは作成失敗とは限らない。"""
    try:
        return subprocess.run(
            [RUNPODCTL, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise GuardError(
            "cli_timeout",
            f"runpodctl {' '.join(args[:2])} exceeded {timeout:g} seconds",
        ) from error
    except OSError as error:
        raise GuardError(
            "cli_error", f"runpodctl could not be executed: {safe_error(str(error))}"
        ) from error


def safe_error(value: str) -> str:
    """ログにAPIキーが混入した場合に、その値だけを隠す。"""
    return re.sub(r"rpa_[A-Za-z0-9]+", "<redacted-key>", value)


def payload_records(payload: Any, key: str) -> list[dict[str, Any]]:
    """未知の形式を空一覧と誤認すると、二重作成や削除済みの誤判定につながる。"""
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and set(payload) == {key}:
        records = payload[key]
    elif isinstance(payload, dict) and set(payload) == {"data"}:
        records = payload["data"]
    else:
        raise GuardError("unknown_json_format", f"unrecognized {key} JSON structure")
    if not isinstance(records, list) or any(
        not isinstance(row, dict) for row in records
    ):
        raise GuardError("unknown_json_format", f"{key} must be an array of objects")
    return records


def decode_json(value: str) -> Any:
    """説明文が混ざった出力も、正常なJSONとしては受け取らない。"""

    def reject_constant(constant: str) -> None:
        raise GuardError(
            "invalid_json", f"runpodctl returned a non-JSON number: {constant}"
        )

    try:
        return json.loads(value, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise GuardError(
            "invalid_json", f"runpodctl returned invalid JSON: {error}"
        ) from error


def cli_failure(result: subprocess.CompletedProcess[str]) -> GuardError:
    """CLIの構造化エラーを保ち、認証エラーなどを在庫不足として再試行しない。"""
    message = safe_error(result.stderr.strip() or result.stdout.strip())
    for line in message.splitlines():
        if not line.lstrip().startswith("{"):
            continue
        payload = decode_json(line)
        if not isinstance(payload, dict) or not isinstance(payload.get("error"), str):
            return GuardError(
                "unknown_json_format", "unrecognized CLI error JSON structure"
            )
        if not isinstance(payload.get("code"), str):
            return GuardError("unknown_json_format", "CLI error JSON has no error code")
        details = {key: payload[key] for key in ("id", "status") if key in payload}
        code = payload["code"]
        # GPU作成のGraphQLエラーには在庫専用コードがないため、明示的な在庫不足だけを扱う。
        capacity_messages = ("no longer any instances", "not enough free gpus")
        if code == "graphql_error" and any(
            text in payload["error"].lower() for text in capacity_messages
        ):
            code = "gpu_unavailable"
        return GuardError(code, payload["error"], **details)
    return GuardError(
        "cli_error", message or f"runpodctl exited with {result.returncode}"
    )


def read_json(command: tuple[str, ...]) -> Any:
    """読み取り系CLIの失敗とJSONの不整合を呼び出し元へ返す。"""
    result = run_ctl(*command, "-o", "json")
    if result.returncode != 0:
        raise cli_failure(result)
    return decode_json(result.stdout)


def validate_pod(pod: Any) -> dict[str, Any]:
    """監視対象の識別と状態確認に必要な項目だけを検証する。"""
    if not isinstance(pod, dict) or any(
        not isinstance(pod.get(key), str) or not pod[key]
        for key in ("id", "name", "desiredStatus")
    ):
        raise GuardError(
            "unknown_json_format", "Pod JSON lacks id, name or desiredStatus"
        )
    return pod


def list_pods() -> list[dict[str, Any]]:
    """全Podを取得する。既存Podを自動停止・削除しないことが重要。"""
    records = payload_records(read_json(("pod", "list", "--all")), "pods")
    return [validate_pod(pod) for pod in records]


def is_active(pod: dict[str, Any]) -> bool:
    """停止済みと分かるPod以外は、追加作成の確認対象にする。"""
    desired = str(pod.get("desiredStatus", "")).upper()
    runtime = str(pod.get("runtimeStatus", "")).lower()
    return desired not in {"EXITED", "STOPPED", "TERMINATED", "DEAD"} or runtime in {
        "running",
        "initializing",
    }


def pod_summary(pod: dict[str, Any]) -> dict[str, Any]:
    """接続情報やコンテナ内の環境変数を出力しない。"""
    return {
        key: pod.get(key)
        for key in (
            "id",
            "name",
            "desiredStatus",
            "runtimeStatus",
            "runtimeStatusReason",
        )
    }


def gpu_records() -> list[dict[str, Any]]:
    """現在のGPU価格と在庫をCLIから取得する。"""
    records = payload_records(
        read_json(("gpu", "list", "--include-unavailable")), "gpus"
    )
    if any(
        not isinstance(gpu.get("gpuId") or gpu.get("id"), str)
        or not (gpu.get("gpuId") or gpu.get("id"))
        or not isinstance(gpu.get("available"), bool)
        for gpu in records
    ):
        raise GuardError(
            "unknown_json_format", "GPU JSON lacks an identifier or availability"
        )
    return records


def gpu_name(gpu: dict[str, Any]) -> str:
    """GPUの表示名を取得する。"""
    return str(
        gpu.get("displayName")
        or gpu.get("name")
        or gpu.get("gpuId")
        or gpu.get("id")
        or ""
    )


def gpu_price(gpu: dict[str, Any], cloud_type: str) -> float | None:
    """指定クラウドのオンデマンド価格を取得する。"""
    key = "communityPricePerHr" if cloud_type == "COMMUNITY" else "securePricePerHr"
    if key not in gpu:
        raise GuardError("unknown_json_format", f"GPU JSON lacks {key}")
    value = gpu[key]
    return None if value is None else price_number(value, "GPU price")


def price_number(value: Any, label: str) -> float:
    """NaNや負の価格で料金上限の比較をすり抜けないようにする。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise GuardError("unknown_json_format", f"{label} is not a number") from error
    if isinstance(value, bool) or not math.isfinite(number) or number < 0:
        raise GuardError(
            "unknown_json_format", f"{label} must be finite and nonnegative"
        )
    return number


def pricing(args: argparse.Namespace, compute_price: float) -> dict[str, Any]:
    """短期検証でもコンテナディスクは課金対象なので、GPU価格に加算する。"""
    container_price = (
        args.container_disk_in_gb * DISK_USD_PER_GB_MONTH / STORAGE_MONTH_HOURS
    )
    volume_price = args.volume_in_gb * DISK_USD_PER_GB_MONTH / STORAGE_MONTH_HOURS
    total = compute_price + container_price + volume_price
    return {
        "compute_usd_per_hour": compute_price,
        "container_disk_gb": args.container_disk_in_gb,
        "volume_gb": args.volume_in_gb,
        "container_usd_per_hour_estimate": container_price,
        "volume_usd_per_hour_estimate": volume_price,
        "total_usd_per_hour_estimate": total,
        "cost_until_deadline_usd_estimate": total * args.delete_after_minutes / 60,
        "storage_usd_per_gb_month": DISK_USD_PER_GB_MONTH,
        "storage_month_hours": STORAGE_MONTH_HOURS,
    }


def find_gpu(gpus: list[dict[str, Any]], requested: str) -> dict[str, Any]:
    """GPU名を完全一致で選び、曖昧な自動選択を避ける。"""
    matches = [
        gpu
        for gpu in gpus
        if gpu_name(gpu) == requested
        or gpu.get("gpuId") == requested
        or gpu.get("id") == requested
    ]
    if len(matches) != 1:
        raise GuardError("gpu_not_found", f"GPU not found uniquely: {requested}")
    return matches[0]


def validate_gpu(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """クラウド別の価格とVRAMを検証する。在庫の確定は作成APIに任せる。"""
    gpu = find_gpu(gpu_records(), args.gpu_id)
    price = gpu_price(gpu, args.cloud_type)
    if price is None:
        raise GuardError(
            "gpu_unavailable", f"No {args.cloud_type} price is available: {args.gpu_id}"
        )
    costs = pricing(args, price)
    if costs["total_usd_per_hour_estimate"] > args.max_hourly_usd:
        raise GuardError(
            "price_limit",
            f"GPU and storage estimate exceeds hourly limit ${args.max_hourly_usd:g}",
            pricing=costs,
        )
    memory = gpu.get("memoryInGb")
    if args.min_vram_gb and (
        memory is None or price_number(memory, "GPU VRAM") < args.min_vram_gb
    ):
        raise GuardError(
            "insufficient_vram",
            f"GPU VRAM {memory} GB is below required {args.min_vram_gb} GB",
        )
    return gpu, costs


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """VPS側のsystemdにタイマー状態を確認する。"""
    return subprocess.run(
        [SYSTEMCTL, *args], capture_output=True, text=True, check=False, timeout=30
    )


def require_creation_environment() -> None:
    """削除タイマーを使えない環境では、作成APIや設定ファイルに触れる前に止める。"""
    if sys.platform != "linux":
        raise GuardError("unsupported_environment", "Pod creation requires Linux")
    if os.geteuid() != 0:
        raise GuardError("unsupported_environment", "Pod creation requires root")
    if not Path(SYSTEMCTL).is_file() or not Path(PYTHON).is_file():
        raise GuardError(
            "unsupported_environment",
            "Pod creation requires systemd and /usr/bin/python3",
        )
    # is-system-runningでは、関係のないサービスの障害でdegradedとなった環境も拒否してしまう。
    try:
        manager = systemctl("show", "--property=Version", "--value")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuardError(
            "unsupported_environment",
            "Cannot connect to the systemd system manager",
            reason=safe_error(str(error)),
        ) from error
    if manager.returncode != 0 or not manager.stdout.strip():
        raise GuardError(
            "unsupported_environment",
            "Cannot connect to the systemd system manager",
            reason=safe_error(manager.stderr.strip() or manager.stdout.strip()),
        )


def unique_pod_name(base: str) -> str:
    """監視対象を既存Podと取り違えないよう、一意な名前を付ける。"""
    stem = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "gpu-check"
    return f"{stem[:32].rstrip('-')}-guard-{uuid.uuid4().hex[:10]}"


def watchdog_units(pod_name: str) -> tuple[str, str] | None:
    """このガードが作ったPod名からsystemd unit名を得る。"""
    match = re.search(r"-guard-([0-9a-f]{10})$", pod_name)
    if not match:
        return None
    unit = f"runpod-gpu-guard-{match.group(1)}"
    return f"{unit}.service", f"{unit}.timer"


def watchdog_active(timer_unit: str) -> bool:
    """停止予定がsystemdに登録され、有効になっていることを確認する。"""
    active = systemctl("is-active", timer_unit)
    enabled = systemctl("is-enabled", timer_unit)
    next_run = systemctl(
        "show", timer_unit, "--property=NextElapseUSecRealtime", "--value"
    )
    return (
        active.returncode == 0
        and active.stdout.strip() == "active"
        and enabled.returncode == 0
        and enabled.stdout.strip() == "enabled"
        and bool(next_run.stdout.strip())
        and next_run.stdout.strip() not in {"n/a", "0"}
    )


def install_watchdog(pod_name: str, deadline: datetime) -> tuple[str, str]:
    """Pod作成前に、再起動後も残る削除タイマーを登録して実動確認する。"""
    script_path = Path(__file__).resolve()
    safe_path = re.compile(r"[A-Za-z0-9_./-]+\Z")
    if not all(safe_path.fullmatch(path) for path in (RUNPODCTL, str(script_path))):
        raise RuntimeError(
            "watchdog paths contain characters unsafe for a systemd unit"
        )

    service_unit, timer_unit = watchdog_units(pod_name) or (None, None)
    if service_unit is None or timer_unit is None:
        raise RuntimeError("invalid guarded pod name")
    service_path = SYSTEMD_DIR / service_unit
    timer_path = SYSTEMD_DIR / timer_unit
    if service_path.exists() or timer_path.exists():
        raise RuntimeError("watchdog unit already exists")

    service_text = (
        "[Unit]\n"
        f"Description=Delete RunPod {pod_name} at its local safety deadline\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "StartLimitIntervalSec=0\n"
        "\n[Service]\n"
        "Type=oneshot\n"
        "User=root\n"
        "Environment=HOME=/root\n"
        f"Environment=RUNPOD_GUARD_RUNPODCTL={RUNPODCTL}\n"
        f"ExecStart={PYTHON} {script_path} watchdog-cleanup --name {pod_name}\n"
        "TimeoutStartSec=150s\n"
        "Restart=on-failure\n"
        "RestartSec=30s\n"
    )
    timer_time = deadline.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    timer_text = (
        "[Unit]\n"
        f"Description=Automatic RunPod cleanup deadline for {pod_name}\n"
        "\n[Timer]\n"
        f"OnCalendar={timer_time}\n"
        "Persistent=true\n"
        "AccuracySec=1s\n"
        f"Unit={service_unit}\n"
        "\n[Install]\n"
        "WantedBy=timers.target\n"
    )

    try:
        service_path.write_text(service_text, encoding="utf-8")
        timer_path.write_text(timer_text, encoding="utf-8")
        service_path.chmod(0o644)
        timer_path.chmod(0o644)
        for command in (("daemon-reload",), ("enable", "--now", timer_unit)):
            result = systemctl(*command)
            if result.returncode != 0:
                raise RuntimeError(
                    safe_error(result.stderr.strip() or result.stdout.strip())
                )
        if not watchdog_active(timer_unit):
            raise RuntimeError(
                "systemd did not confirm an enabled future cleanup deadline"
            )

        # 有料Podを作る前に、監視サービス自身の認証とAPI接続を確かめる。
        probe = systemctl("start", service_unit)
        service_result = systemctl("show", service_unit, "--property=Result", "--value")
        if probe.returncode != 0 or service_result.stdout.strip() != "success":
            raise RuntimeError("systemd watchdog could not verify RunPod API access")
        if not watchdog_active(timer_unit):
            raise RuntimeError("watchdog deadline became inactive during its API check")
    except Exception:
        systemctl("disable", "--now", timer_unit)
        service_path.unlink(missing_ok=True)
        timer_path.unlink(missing_ok=True)
        systemctl("daemon-reload")
        raise

    return service_unit, timer_unit


def ensure_duration(value: int, maximum: int, label: str) -> None:
    """自動停止・削除までの時間に安全上の上限を設ける。"""
    if not 1 <= value <= maximum:
        raise GuardError(
            "usage_error", f"{label} must be between 1 and {maximum} minutes"
        )


def wait_seconds(value: str) -> float:
    """SSH待機時間も外側の実行時間制限に含める。s・m・h・msの組み合わせを受け付ける。"""
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|s|m|h))+", value):
        raise GuardError(
            "usage_error", "--wait-timeout must be a duration such as 90s or 3m"
        )
    factors = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    seconds = sum(
        float(amount) * factors[unit]
        for amount, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    )
    if not 0 < seconds <= MAX_LIFETIME_MINUTES * 60:
        raise GuardError(
            "usage_error", "--wait-timeout must be positive and at most 60m"
        )
    return seconds


def create_command(args: argparse.Namespace, gpu_id: str, pod_name: str) -> list[str]:
    """購入条件をCLIへ渡す。永続ボリュームは明示された容量だけ付ける。"""
    command = [
        "pod",
        "create",
        "--compute-type",
        "GPU",
        "--cloud-type",
        args.cloud_type,
        "--gpu-id",
        gpu_id,
        "--gpu-count",
        "1",
        "--image",
        args.image,
        "--container-disk-in-gb",
        str(args.container_disk_in_gb),
        "--volume-in-gb",
        str(args.volume_in_gb),
        "--name",
        pod_name,
        "-o",
        "json",
    ]
    if args.data_center_ids:
        command.extend(("--data-center-ids", args.data_center_ids))
    if args.ports:
        command.extend(("--ports", args.ports))
    if args.nvidia_driver_capabilities:
        command.extend(
            (
                "--env",
                json.dumps(
                    {"NVIDIA_DRIVER_CAPABILITIES": args.nvidia_driver_capabilities},
                    separators=(",", ":"),
                ),
            )
        )
    if args.public_ip:
        command.append("--public-ip")
    if args.wait:
        command.extend(("--wait", "--wait-timeout", args.wait_timeout))
    return command


def command_create(args: argparse.Namespace) -> dict[str, Any]:
    """確定時だけ最大3回試す。予算・GPU・削除期限を再試行のために緩めない。"""
    ensure_duration(
        args.delete_after_minutes, MAX_LIFETIME_MINUTES, "delete-after-minutes"
    )
    if not math.isfinite(args.max_hourly_usd) or args.max_hourly_usd <= 0:
        raise GuardError("usage_error", "--max-hourly-usd must be finite and positive")
    if not math.isfinite(args.min_vram_gb) or args.min_vram_gb < 0:
        raise GuardError("usage_error", "--min-vram-gb must be finite and nonnegative")
    if args.container_disk_in_gb <= 0 or args.volume_in_gb < 0:
        raise GuardError(
            "usage_error", "container disk must be positive; volume must be nonnegative"
        )
    if args.wait and args.cloud_type == "COMMUNITY" and not args.public_ip:
        raise GuardError("usage_error", "--wait with COMMUNITY requires --public-ip")
    if args.wait and "22/tcp" not in args.ports.split(","):
        raise GuardError("usage_error", "--wait requires 22/tcp in --ports")
    timeout = CLI_TIMEOUT_SECONDS + (
        wait_seconds(args.wait_timeout) if args.wait else 0
    )
    if args.confirm:
        require_creation_environment()
    warnings = []
    if args.allow_multiple:
        warnings.append(
            "Multiple Pods are allowed; the hourly limit applies only to this new Pod, not the account total."
        )
        print("warning: " + warnings[0], file=sys.stderr)

    pod_name = unique_pod_name(args.name)
    timer_unit = None
    deadline = None
    attempts = CREATE_ATTEMPTS if args.confirm else 1
    retry_codes = {
        "price_limit",
        "gpu_unavailable",
        "supply_constraint",
        "rate_limited",
    }
    for attempt in range(1, attempts + 1):
        try:
            pods = list_pods()
            same_name = [pod_summary(pod) for pod in pods if pod["name"] == pod_name]
            if same_name:
                raise GuardError(
                    "pod_already_exists",
                    "this create attempt already has a Pod",
                    matched_pods=same_name,
                )
            active = [pod_summary(pod) for pod in pods if is_active(pod)]
            if active and not args.allow_multiple:
                raise GuardError(
                    "active_pods",
                    "active Pods exist; use --allow-multiple only if intentional",
                    existing_pods=active,
                )
            gpu, costs = validate_gpu(args)
            # CLIのavailableはクラウド別の在庫ではないため、作成を拒否する根拠にしない。
            if not gpu["available"]:
                warning = (
                    "The CLI reports no available GPU, but this is not cloud-specific; "
                    "the create API will determine capacity."
                )
                if warning not in warnings:
                    warnings.append(warning)
                    print("warning: " + warning, file=sys.stderr)
            command = create_command(args, str(gpu.get("gpuId") or gpu["id"]), pod_name)
            plan = {
                "dry_run": not args.confirm,
                "pod_name": pod_name,
                "cloud_type": args.cloud_type,
                "gpu": gpu_name(gpu),
                "available": gpu["available"],
                "existing_pods": [pod_summary(pod) for pod in pods],
                "pricing": costs,
                "max_hourly_usd": args.max_hourly_usd,
                "delete_after_minutes": args.delete_after_minutes,
                "planned_command": [RUNPODCTL, *command],
                "warnings": warnings,
                "storage_note": "Persistent volume is normally unnecessary for short tests; use --volume-in-gb only when needed. Container disk is always needed.",
            }
            if not args.confirm:
                return plan

            if deadline is None:
                deadline = datetime.now(timezone.utc) + timedelta(
                    minutes=args.delete_after_minutes
                )
                _, timer_unit = install_watchdog(pod_name, deadline)
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise GuardError(
                    "deadline_expired",
                    "cleanup deadline passed; refusing another create attempt",
                )
            if not watchdog_active(timer_unit):
                watchdog_cleanup(pod_name)
                raise GuardError(
                    "watchdog_inactive", "local cleanup timer is no longer active"
                )

            # 作成APIのタイムアウト後はPodが存在し得るため、自動再試行しない。
            try:
                result = run_ctl(*command, timeout=min(timeout, remaining))
                if result.stderr.strip() and result.returncode == 0:
                    print(safe_error(result.stderr.strip()), file=sys.stderr)
                if result.returncode != 0:
                    raise cli_failure(result)
                pod = validate_pod(decode_json(result.stdout))
                if pod["name"] != pod_name:
                    raise GuardError(
                        "unknown_json_format",
                        "created Pod name differs from the requested name",
                        id=pod["id"],
                        returned_pod_name=pod["name"],
                    )
            except GuardError as error:
                try:
                    matches = [
                        pod_summary(pod)
                        for pod in list_pods()
                        if pod["name"] == pod_name
                    ]
                except GuardError as lookup_error:
                    # 一覧取得の失敗を再試行可能な作成失敗に置き換えると、二重購入し得る。
                    raise GuardError(
                        "creation_uncertain",
                        "create failed and the resulting Pod could not be checked; do not repeat create",
                        create_error={
                            "code": error.code,
                            "message": safe_error(str(error)),
                            **error.details,
                        },
                        lookup_error={
                            "code": lookup_error.code,
                            "message": safe_error(str(lookup_error)),
                        },
                    ) from error
                error.details["matched_pods"] = matches
                if matches or error.details.get("id"):
                    error.details["resource_created"] = True
                raise

            # 一覧の価格は割り当て先の価格と異なる場合があるため、作成結果でも確認する。
            try:
                actual_costs = pricing(
                    args, price_number(pod.get("costPerHr"), "Pod costPerHr")
                )
                if any(
                    pod.get(key) != value
                    for key, value in (
                        ("containerDiskInGb", args.container_disk_in_gb),
                        ("volumeInGb", args.volume_in_gb),
                    )
                ):
                    raise GuardError(
                        "allocation_mismatch",
                        "allocated disk sizes differ from the requested sizes",
                    )
                if actual_costs["total_usd_per_hour_estimate"] > args.max_hourly_usd:
                    raise GuardError(
                        "price_limit",
                        "allocated Pod and storage estimate exceeds the hourly limit",
                        pricing=actual_costs,
                    )
            except GuardError as error:
                try:
                    stop_and_delete_pod(pod["id"])
                    remains = any(item["id"] == pod["id"] for item in list_pods())
                except GuardError as cleanup_error:
                    raise GuardError(
                        "cleanup_incomplete",
                        f"rejected Pod deletion could not be confirmed: {safe_error(str(cleanup_error))}",
                        id=pod["id"],
                    ) from error
                if remains:
                    raise GuardError(
                        "cleanup_incomplete",
                        "rejected Pod remains after deletion",
                        id=pod["id"],
                    ) from error
                error.details["deleted_pod_id"] = pod["id"]
                raise
            if not watchdog_active(timer_unit):
                watchdog_cleanup(pod_name)
                raise GuardError(
                    "watchdog_inactive", "local cleanup timer is no longer active"
                )
            return {
                **plan,
                "pod": pod_summary(pod),
                "pricing": actual_costs,
                "attempts": attempt,
                "watchdog_timer": timer_unit,
                "deletes_at": deadline.isoformat(),
            }
        except GuardError as error:
            error.details.update(attempts=attempt, pod_name=pod_name)
            if timer_unit:
                error.details.update(
                    watchdog_timer=timer_unit, deletes_at=deadline.isoformat()
                )
            if (
                error.code not in retry_codes
                or error.details.get("resource_created")
                or attempt == attempts
            ):
                raise
            print(
                f"retry: attempt {attempt}/{attempts}: {safe_error(str(error))}; waiting {RETRY_DELAY_SECONDS}s without raising the price limit",
                file=sys.stderr,
            )
            time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError("create attempts exhausted unexpectedly")


def stop_and_delete_pod(pod_id: str) -> None:
    """停止直後に削除する。削除エラー時も、既にPodが消えていれば成功とみなす。"""
    try:
        stop = run_ctl("pod", "stop", pod_id)
        if stop.returncode:
            print(
                f"warning: Pod stop failed; still attempting deletion: {safe_error(str(cli_failure(stop)))}",
                file=sys.stderr,
            )
    except GuardError as error:
        print(
            f"warning: Pod stop failed; still attempting deletion: {safe_error(str(error))}",
            file=sys.stderr,
        )
    delete = run_ctl("pod", "delete", pod_id)
    if delete.returncode != 0 and any(pod.get("id") == pod_id for pod in list_pods()):
        raise cli_failure(delete)


def watchdog_cleanup(pod_name: str) -> list[str]:
    """期限到達時に、作成時に割り当てた名前のPodだけを削除する。"""
    matches = [pod for pod in list_pods() if pod.get("name") == pod_name]
    for pod in matches:
        stop_and_delete_pod(pod["id"])
    if any(pod.get("name") == pod_name for pod in list_pods()):
        raise RuntimeError(
            f"RunPod still lists guarded Pod {pod_name}; service will retry"
        )
    return [pod["id"] for pod in matches]


def command_watchdog_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    """systemd timerから呼び出す削除処理。"""
    return {"pod_name": args.name, "deleted_pod_ids": watchdog_cleanup(args.name)}


def command_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    """指定されたPodだけを停止して削除する。"""
    if not args.confirm:
        raise GuardError("usage_error", "refusing cleanup without --confirm")
    stop_and_delete_pod(args.pod_id)
    if any(pod.get("id") == args.pod_id for pod in list_pods()):
        raise RuntimeError(f"Pod {args.pod_id} remains after deletion")
    return {"deleted_pod_id": args.pod_id}


def command_preflight(args: argparse.Namespace) -> dict[str, Any]:
    """作成前に既存Podと指定GPUだけを読み取り検査する。"""
    pods = list_pods()
    summary: dict[str, Any] = {"existing_pods": [pod_summary(pod) for pod in pods]}
    if args.gpu_id:
        gpu = find_gpu(gpu_records(), args.gpu_id)
        summary["gpu"] = {
            "name": gpu_name(gpu),
            "gpuId": gpu.get("gpuId") or gpu.get("id"),
            "memoryInGb": gpu.get("memoryInGb"),
            "communityPricePerHr": gpu.get("communityPricePerHr"),
            "securePricePerHr": gpu.get("securePricePerHr"),
            "available": gpu.get("available"),
            "stockStatus": gpu.get("stockStatus"),
        }
    return summary


class JSONArgumentParser(argparse.ArgumentParser):
    """引数エラーも、他の失敗と同じJSON形式で返す。"""

    def error(self, message: str) -> None:
        raise GuardError("usage_error", message)


def parser() -> argparse.ArgumentParser:
    """安全側の既定値を持つコマンドラインを構築する。"""
    root = JSONArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--gpu-id")
    preflight.set_defaults(handler=command_preflight)

    create = subparsers.add_parser("create")
    create.add_argument("--gpu-id", required=True)
    create.add_argument("--image", required=True)
    create.add_argument(
        "--cloud-type", choices=("SECURE", "COMMUNITY"), default="SECURE"
    )
    create.add_argument(
        "--max-hourly-usd",
        type=float,
        required=True,
        help="hourly limit for this new Pod, including estimated disk charges",
    )
    create.add_argument("--min-vram-gb", type=float, default=0)
    create.add_argument("--container-disk-in-gb", type=int, default=20)
    create.add_argument(
        "--volume-in-gb",
        type=int,
        default=0,
        help="persistent volume size; normally unnecessary for short tests",
    )
    create.add_argument(
        "--allow-multiple",
        action="store_true",
        help="allow another Pod with a warning; no account-wide budget limit",
    )
    create.add_argument("--name", default="runpod-gpu-check")
    create.add_argument("--data-center-ids")
    create.add_argument("--ports", default="22/tcp")
    create.add_argument(
        "--nvidia-driver-capabilities",
        choices=("compute,utility", "graphics,compute,utility"),
        help="NVIDIA container driver capabilities needed inside the Pod",
    )
    create.add_argument("--public-ip", action="store_true")
    create.add_argument("--delete-after-minutes", type=int, default=30)
    create.add_argument("--wait", action="store_true")
    create.add_argument("--wait-timeout", default="90s")
    create.add_argument("--confirm", action="store_true")
    create.set_defaults(handler=command_create)

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("pod_id")
    cleanup.add_argument("--confirm", action="store_true")
    cleanup.set_defaults(handler=command_cleanup)

    watchdog = subparsers.add_parser("watchdog-cleanup", help=argparse.SUPPRESS)
    watchdog.add_argument("--name", required=True)
    watchdog.set_defaults(handler=command_watchdog_cleanup)
    return root


def main(argv: list[str] | None = None) -> int:
    """標準出力は成功・失敗ともJSONを1つだけ返し、進捗や警告は標準エラーへ出す。"""
    try:
        args = parser().parse_args(argv)
        output = {"ok": True, "command": args.command, **args.handler(args)}
    except GuardError as error:
        output = {
            "ok": False,
            "code": error.code,
            "error": safe_error(str(error)),
            **error.details,
        }
    except subprocess.TimeoutExpired as error:
        output = {
            "ok": False,
            "code": "local_timeout",
            "error": f"local command exceeded {error.timeout} seconds",
        }
    except (OSError, RuntimeError) as error:
        output = {"ok": False, "code": "local_error", "error": safe_error(str(error))}
    except KeyboardInterrupt:
        output = {
            "ok": False,
            "code": "interrupted",
            "error": "interrupted; any installed cleanup timer remains armed",
        }
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if output["ok"] else (2 if output["code"] == "usage_error" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
