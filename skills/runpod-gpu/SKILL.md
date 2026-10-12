---
name: runpod-gpu
description: Run GPU workloads on RunPod within approved budgets, using the existing harness for Pod creation, inspection, and cleanup.
license: MIT
---

# RunPod GPU

## Harness

Use the bundled `scripts/runpod_gpu_guard.py` and read [the usage guide](references/harness.md) for syntax. Resolve paths relative to this skill's directory. Below, `preflight`, `create`, and `cleanup` are subcommands of `python3 runpod_gpu_guard.py`, run from `scripts/`.

The local harness needs Python 3.10+ and authenticated runpodctl. Confirmed creation also needs Linux, root, and working systemd; do not bypass these checks. Keep credentials out of logs and upload bundles.

## Plan and create

1. Confirm permission to spend, the storage-inclusive hourly cap, and maximum lifetime; respect any total budget. Ask only for missing limits, not approval already granted for this task.
2. Prepare inputs, commands, SSH credentials, and a local result destination before renting. Budget time for startup and transfer. Choose a container image that supports the workload and GPU drivers before renting; upload only required code and inputs.
3. Check existing Pods with `preflight`. Get current offers with `runpodctl gpu list --include-unavailable -o json`; select the cheapest GPU meeting VRAM and software requirements, including Community Cloud. Use the selected cloud's price. The CLI's `available` flag is not cloud-specific; `false` does not prove that creation will fail.
4. Inspect a quote with `create` without `--confirm`. Container disk is required; persistent volume defaults to zero and is normally unnecessary. Include requested storage in the estimate.
5. Use `create --confirm` with explicit `--max-hourly-usd` and `--delete-after-minutes` (currently at most 60 minutes). Never run `planned_command` directly: it bypasses the guard. Record the Pod ID, unique name, timer, and deadline.

Leave unrelated Pods untouched. Use `--allow-multiple` only with explicit permission: limits are per Pod, not account-wide, and concurrent agents are not coordinated. Each agent owns its Pod's cleanup.

## Run and recover

- Omit `--wait` to validate the allocated price before bounded startup polling with `runpodctl pod get POD_ID`. If using `--wait`, allow cold-start time within the cleanup deadline; Community Cloud needs `--public-ip` and exposed `22/tcp`. This checks an SSH banner, not authentication or GPU execution.
- Obtain connection details with `runpodctl ssh info POD_ID`, connect using existing SSH credentials, and run the prepared workload. Verify only the requested level: allocation, startup, or actual GPU execution. When timing is requested, separate startup from execution.
- The harness makes up to three attempts for capacity, price-limit, and rate-limit failures without relaxing the GPU, budget, or installed deadline. After capacity retries, try another qualifying offer only after confirming no Pod remains from the previous attempt. Stop if none fits the budget.
- Creation/wait timeouts can leave billable Pods. Inspect the returned ID/name with `preflight`; do not blindly recreate. Authentication errors and unknown JSON are not retryable. If Pod existence or deletion is uncertain, stop new purchases and report it.
- Stdout is one JSON document; warnings/progress use stderr. Failed lookups and unknown JSON are not empty inventories or proof of deletion.

## Finish

Save required results locally and verify transfer. On success, failure, or interruption, use `cleanup POD_ID --confirm` and verify the target is absent with `preflight`; do not wait for the timer. Keep the timer until deletion is confirmed. `cleanup` deletes the Pod, not its systemd units; when removing those manually, target only the recorded units for this attempt.

The watchdog depends on the local host remaining available. Storage costs are estimates and deletion can finish after the scheduled deadline; neither guarantees a final bill.

Report the outcome, GPU/rate, relevant timings, saved results, and deletion status. Distinguish spending estimates from verified charges.
