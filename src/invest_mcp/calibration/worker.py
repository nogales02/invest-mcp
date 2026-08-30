"""Calibration worker -- RUNS IN THE ``invest-cal`` CONDA ENV.

Invoked by :mod:`invest_mcp.calibration.client` as::

    <invest-cal>/python.exe -m invest_mcp.calibration.worker   < request.json

``request`` (stdin, JSON)::

    {"action": "validate",  "config": {...}}
    {"action": "calibrate", "config": {...}, "progress_path": "<file>"}

For ``validate`` the worker prints ``{"issues": [...]}``.
For ``calibrate`` it appends one JSON line per iteration to ``progress_path`` and,
when done, writes the full result to ``<workspace_dir>/calibration_result.json``
and echoes it on stdout.

Only stdlib is imported at module load; the heavy
``invest_calibration_assistant.core`` import happens inside ``main``.
"""

from __future__ import annotations

import json
import os
import sys
import traceback


def _emit_progress(path):
    def cb(it, n, objective, params):
        rec = {"iter": it, "n": n, "objective": objective, "params": params}
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass
    return cb


def main() -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"bad request json: {exc}"}, sys.stdout)
        return 2

    action = req.get("action", "calibrate")
    config = req.get("config") or {}

    try:
        from invest_calibration_assistant.core import calibrate, validate_config
    except Exception as exc:  # noqa: BLE001
        json.dump({"ok": False, "error": f"invest_calibration_assistant not importable "
                   f"in this env: {exc}"}, sys.stdout)
        return 3

    if action == "validate":
        json.dump({"ok": True, "issues": validate_config(config)}, sys.stdout)
        return 0

    progress_path = req.get("progress_path")
    cb = _emit_progress(progress_path) if progress_path else None
    try:
        result = calibrate(config, progress_cb=cb, log=lambda m: print(m, flush=True))
    except Exception:  # noqa: BLE001
        result = {"ok": False, "stage": "engine", "error": traceback.format_exc()}

    ws = config.get("workspace_dir")
    if ws:
        try:
            with open(os.path.join(ws, "calibration_result.json"), "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2, default=str)
        except OSError:
            pass
    json.dump(result, sys.stdout, default=str)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
