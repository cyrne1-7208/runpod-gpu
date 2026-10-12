# RunPod GPU

A Codex skill and a Python harness for RunPod GPU jobs, with price checks, bounded retries, and scheduled cleanup.

## Contents

- [Skill](skills/runpod-gpu/SKILL.md): instructions for planning, running, and cleaning up GPU jobs.
- [Harness](skills/runpod-gpu/scripts/runpod_gpu_guard.py): Pod creation, inspection, and deletion.
- [Usage guide](skills/runpod-gpu/references/harness.md): prerequisites, commands, pricing estimates, and limitations.

The `skills/runpod-gpu` directory contains the skill and all its supporting files.

## Usage

Python 3.10+ and authenticated [runpodctl](https://github.com/runpod/runpodctl) are required. Pod creation additionally requires Linux, root, and systemd on the local host.

```bash
cd skills/runpod-gpu/scripts
python3 runpod_gpu_guard.py --help
python3 runpod_gpu_guard.py preflight
python3 runpod_gpu_guard.py create --help
```

Creation requires `--confirm` and an explicit hourly limit. Read the usage guide before creating a paid Pod. Cleanup timers depend on the local host; they do not guarantee cleanup while that host is offline.

## Tests

Run from the repository root:

```bash
python3 -B -m unittest discover -s skills/runpod-gpu/scripts -q
```

These tests do not create paid Pods or modify the host's systemd configuration.

## License

[MIT](LICENSE).
