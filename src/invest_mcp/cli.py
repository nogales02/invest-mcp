"""``invest-mcp`` command line: run the server, or set up / check the install.

Client-agnostic. The server speaks the MCP protocol over stdio (default) or
streamable-http; any MCP-capable client works (Claude Desktop/Code, Cline,
Continue, LibreChat, mcphost for Ollama, the OpenAI Agents SDK, ...).

    invest-mcp                 run the server (stdio)
    invest-mcp serve --transport streamable-http --host 0.0.0.0 --port 8000
    invest-mcp doctor          check invest.exe + the invest-geo / invest-cal envs
    invest-mcp setup           create the conda envs from environment-*.yml
    invest-mcp mcp-config      print the JSON / command to register with a client
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from invest_mcp import __version__
from invest_mcp.config import detect_cal_python, detect_geo_python, get_settings

_REPO = Path(__file__).resolve().parents[2]           # repo root when run from a checkout
_PLUGIN_SPEC_DEFAULT = os.environ.get(
    "INVEST_MCP_CAL_PLUGIN_SPEC",
    "invest-calibration-assistant @ git+https://github.com/N4W-Facility/"
    "Invest_Plugin_Calibration.git@refactor/shared-core",
)


# ---------------------------------------------------------------------------
def _find_conda() -> str | None:
    """A real conda/mamba/micromamba **executable** (never a ``condabin`` .BAT/.CMD
    wrapper -- those crash on `env create` on some Windows setups)."""
    from invest_mcp.config import _conda_env_roots

    for var in ("MAMBA_EXE", "CONDA_EXE"):
        v = os.environ.get(var)
        if v and Path(v).is_file() and Path(v).suffix.lower() not in (".bat", ".cmd"):
            return v
    # micromamba is a single static binary -> always safe
    mm = shutil.which("micromamba")
    if mm:
        return mm
    # the InVEST Workbench bundles one
    for base in (Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")):
        for hit in base.glob("InVEST *Workbench/resources/micromamba.exe"):
            return str(hit)
    # real conda/mamba exe inside a detected install root
    for root in _conda_env_roots():
        for rel in ("Scripts/mamba.exe", "Scripts/conda.exe", "condabin/mamba.exe",
                    "condabin/conda.exe", "bin/mamba", "bin/conda"):
            cand = root / rel
            if cand.is_file():
                return str(cand)
    # last resort: PATH, but skip .bat/.cmd shims
    for name in ("mamba", "conda"):
        found = shutil.which(name)
        if found and Path(found).suffix.lower() not in (".bat", ".cmd"):
            return found
    return None


def _env_create_cmd(conda: str, yml: Path) -> list[str]:
    if "micromamba" in Path(conda).name.lower():
        return [conda, "create", "-y", "-f", str(yml)]
    return [conda, "env", "create", "-y", "-f", str(yml)]


def _env_python_via(conda: str, name: str) -> Path | None:
    """Ask the conda tool itself for an env's python (covers non-standard roots
    like micromamba's %APPDATA%\\mamba)."""
    try:
        cp = subprocess.run(
            [conda, "run", "-n", name, "python", "-c",
             "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=120,
        )
        p = Path(cp.stdout.strip())
        return p if cp.returncode == 0 and p.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def _run(cmd: list[str], **kw) -> int:
    if cmd and Path(cmd[0]).suffix.lower() in (".bat", ".cmd"):
        cmd = ["cmd", "/c", *cmd]
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, **kw).returncode


def _probe(python: Path, imports: str) -> tuple[bool, str]:
    try:
        cp = subprocess.run(
            [str(python), "-c", f"import {imports}; print('ok')"],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    out = (cp.stdout + cp.stderr).strip()
    last = out.splitlines()[-1] if out else ""
    return cp.returncode == 0, (last or "ok")


# ---------------------------------------------------------------------------
def cmd_doctor(_args) -> int:
    s = get_settings()
    ok = True
    print(f"invest-mcp {__version__}\n")

    # invest.exe
    try:
        exe = s.resolved_invest_exe
        cp = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                            timeout=30, env={**os.environ, "PYTHONUTF8": "1",
                                             "PYTHONIOENCODING": "utf-8"})
        ver = (cp.stdout or cp.stderr).strip()
        print(f"[ok]   invest      {exe}  (v{ver})")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] invest      {exc}")

    # invest-geo
    gp = detect_geo_python()
    if gp is None:
        print("[warn] invest-geo  not found  -> preflight_geo disabled  "
              "(run: invest-mcp setup --geo)")
    else:
        good, msg = _probe(gp, "rasterio, pyproj, shapely, pyogrio")
        print(f"[{'ok' if good else 'FAIL'}]{'   ' if good else ' '}invest-geo  {gp}"
              + ("" if good else f"\n         {msg}"))
        ok = ok and good

    # invest-cal
    cp_ = detect_cal_python()
    if cp_ is None:
        print("[warn] invest-cal  not found  -> calibration disabled  "
              "(run: invest-mcp setup --cal)")
    else:
        good, msg = _probe(cp_, "natcap.invest, spotpy, invest_calibration_assistant.core")
        print(f"[{'ok' if good else 'FAIL'}]{'   ' if good else ' '}invest-cal  {cp_}"
              + ("" if good else f"\n         {msg}"))
        ok = ok and good

    # data root
    try:
        s.data_root.mkdir(parents=True, exist_ok=True)
        print(f"[ok]   data root   {s.data_root}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] data root   {s.data_root}  ({exc})")

    print("\nallowed input roots:")
    for r in s.allowed_roots():
        print(f"  - {r}")
    print("\n" + ("all good" if ok else "some checks failed -- see above"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def cmd_setup(args) -> int:
    conda = args.conda or _find_conda()
    if not conda:
        print("No conda/mamba/micromamba found. Install Miniforge or micromamba, "
              "or pass --conda <path>.", file=sys.stderr)
        return 2
    do_geo = args.geo or not args.cal
    do_cal = args.cal or not args.geo
    print(f"using: {conda}\n")
    rc = 0
    if do_geo:
        rc |= _run(_env_create_cmd(conda, _REPO / "environment-geo.yml"))
        gp = detect_geo_python() or _env_python_via(conda, "invest-geo")
        if gp:
            print(f"invest-geo python: {gp}")
            rc |= _run([str(gp), "-m", "pip", "install", "-e", str(_REPO), "--no-deps"])
        else:
            print("! invest-geo not found after create -- check the log above")
            rc |= 1
    if do_cal:
        rc |= _run(_env_create_cmd(conda, _REPO / "environment-cal.yml"))
        cp_ = detect_cal_python() or _env_python_via(conda, "invest-cal")
        if cp_:
            print(f"invest-cal python: {cp_}")
            rc |= _run([str(cp_), "-m", "pip", "install", "spotpy>=1.6.2",
                        args.plugin_spec])
            rc |= _run([str(cp_), "-m", "pip", "install", "-e", str(_REPO), "--no-deps"])
        else:
            print("! invest-cal not found after create -- check the log above")
            rc |= 1
    print("\nsetup " + ("done" if rc == 0 else f"finished with errors (rc={rc})"))
    if rc == 0:
        print("next: invest-mcp doctor")
    else:
        print("some steps failed; fix and re-run, or set INVEST_MCP_GEO_PYTHON / "
              "INVEST_MCP_CAL_PYTHON to the env's python.")
    return 0 if rc == 0 else 1


# ---------------------------------------------------------------------------
def cmd_mcp_config(args) -> int:
    py = sys.executable
    env: dict[str, str] = {}
    try:
        env["INVEST_MCP_INVEST_EXE"] = str(get_settings().resolved_invest_exe)
    except Exception:  # noqa: BLE001
        pass
    if args.client == "claude-code":
        parts = ["claude", "mcp", "add", "invest", "--scope", "user"]
        for k, v in env.items():
            parts += ["--env", f"{k}={v}"]
        parts += ["--", py, "-m", "invest_mcp"]
        print(" ".join(f'"{p}"' if " " in p else p for p in parts))
    else:
        block = {"mcpServers": {"invest": {"command": py, "args": ["-m", "invest_mcp"]}}}
        if env:
            block["mcpServers"]["invest"]["env"] = env
        print(json.dumps(block, indent=2))
    return 0


# ---------------------------------------------------------------------------
def cmd_serve(args) -> int:
    from invest_mcp.server import run

    run(transport=args.transport, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="invest-mcp", description=__doc__)
    p.add_argument("--version", action="version", version=f"invest-mcp {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", help="run the MCP server")
    sp.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default=None)
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(func=cmd_serve)

    sub.add_parser("doctor", help="check invest.exe + the conda envs").set_defaults(func=cmd_doctor)

    su = sub.add_parser("setup", help="create the invest-geo / invest-cal conda envs")
    su.add_argument("--geo", action="store_true", help="only the invest-geo env")
    su.add_argument("--cal", action="store_true", help="only the invest-cal env")
    su.add_argument("--conda", default=None, help="path to conda/mamba/micromamba")
    su.add_argument("--plugin-spec", default=_PLUGIN_SPEC_DEFAULT,
                    help="pip spec for invest-calibration-assistant")
    su.set_defaults(func=cmd_setup)

    mc = sub.add_parser("mcp-config", help="print client registration config")
    mc.add_argument("--client", choices=["generic", "claude-code"], default="generic")
    mc.set_defaults(func=cmd_mcp_config)

    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        from invest_mcp.server import run

        run()
        return
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
