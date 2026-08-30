#!/usr/bin/env bash
# One-shot installer for invest-mcp on Linux / macOS.
#
# You do NOT need a system Python: with --conda-server (the default when no
# Python >= 3.10 is found) the server goes in a conda/micromamba env too.
#
# Prereqs you install yourself: the InVEST Workbench OR a conda-forge
# `natcap.invest` on PATH, and a conda/mamba/micromamba (Miniforge is fine; the
# InVEST Workbench also bundles a micromamba).
#
# Usage:
#   scripts/bootstrap.sh                  # auto: .venv if system Python, else conda env
#   scripts/bootstrap.sh --conda-server   # force the server into a conda env
#   scripts/bootstrap.sh --venv           # force a .venv (needs system Python >= 3.10)
#   scripts/bootstrap.sh --skip-envs      # no invest-geo / invest-cal
#   scripts/bootstrap.sh --geo | --cal    # only that sidecar env
#   scripts/bootstrap.sh --conda=/path/to/micromamba
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

say()  { printf '==> %s\n' "$*"; }
ok()   { printf '    [ok] %s\n' "$*"; }
warn() { printf '    [!!] %s\n' "$*" >&2; }
die()  { printf '    [XX] %s\n' "$*" >&2; exit 1; }

FORCE_CONDA=0; FORCE_VENV=0; SKIP_ENVS=0; CONDA=""; SETUP_ARGS=()
for a in "$@"; do
  case "$a" in
    --conda-server) FORCE_CONDA=1 ;;
    --venv) FORCE_VENV=1 ;;
    --skip-envs) SKIP_ENVS=1 ;;
    --geo) SETUP_ARGS+=(--geo) ;;
    --cal) SETUP_ARGS+=(--cal) ;;
    --conda=*) CONDA="${a#*=}"; SETUP_ARGS+=(--conda "${a#*=}") ;;
    *) warn "ignoring unknown arg: $a" ;;
  esac
done
[ "$FORCE_CONDA" -eq 1 ] && [ "$FORCE_VENV" -eq 1 ] && die "--conda-server and --venv are mutually exclusive"

say "invest-mcp bootstrap  (repo: $REPO_ROOT)"
[ -f "$REPO_ROOT/environment-geo.yml" ] || die "run this from a git clone / ZIP of the repo"

find_system_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

find_conda() {
  if [ -n "$CONDA" ]; then echo "$CONDA"; return 0; fi
  for v in "${MAMBA_EXE:-}" "${CONDA_EXE:-}"; do
    [ -n "$v" ] && [ -x "$v" ] && { echo "$v"; return 0; }
  done
  for n in micromamba mamba conda; do
    command -v "$n" >/dev/null 2>&1 && { command -v "$n"; return 0; }
  done
  for p in "$HOME"/*/resources/micromamba /opt/*/resources/micromamba \
           /Applications/InVEST*/Contents/Resources/micromamba; do
    [ -x "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}

conda_create() {  # $1=conda  $2=yml
  case "$(basename "$1")" in
    *micromamba*) "$1" create -y -f "$2" ;;
    *)            "$1" env create -y -f "$2" ;;
  esac
}

# --- 1. decide + build where the server lives -> $PY ----------------------
PY=""
USE_CONDA=$FORCE_CONDA
if [ "$FORCE_CONDA" -eq 0 ] && [ "$FORCE_VENV" -eq 0 ]; then
  if find_system_python >/dev/null; then
    say "system Python >= 3.10 found -> using a .venv (pass --conda-server to use conda)"
  else
    say "no system Python >= 3.10 -> server goes in a conda env (no Python install needed)"
    USE_CONDA=1
  fi
fi

if [ "$USE_CONDA" -eq 1 ]; then
  conda="$(find_conda)" || die "no conda/mamba/micromamba found. Install Miniforge or the InVEST Workbench, or pass --conda=<path>."
  ok "conda tool: $conda"
  say "creating the 'invest-mcp' env ..."
  conda_create "$conda" "$REPO_ROOT/environment-server.yml"
  PY="$("$conda" run -n invest-mcp python -c 'import sys;print(sys.executable)')"
  [ -x "$PY" ] || die "'invest-mcp' env not found after create"
  ok "server python: $PY"
  [ -z "$CONDA" ] && SETUP_ARGS+=(--conda "$conda")
else
  if [ -x "$VENV_PY" ]; then
    ok ".venv already exists"
  else
    sp="$(find_system_python)" || die "no Python >= 3.10 on PATH; re-run with --conda-server"
    ok "using $sp"
    say "creating .venv ..."
    "$sp" -m venv "$VENV_DIR"
  fi
  PY="$VENV_PY"
fi

# --- 2. install --------------------------------------------------------
say "installing invest-mcp ..."
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install -e "$REPO_ROOT"
ok "invest-mcp installed"

# --- 3. sidecar envs -------------------------------------------------
if [ "$SKIP_ENVS" -eq 1 ]; then
  warn "skipping invest-geo / invest-cal: preflight_geo + run_calibration disabled"
else
  say "building invest-geo / invest-cal (slow: 10-25 min) ..."
  "$PY" -m invest_mcp setup "${SETUP_ARGS[@]}" || \
    warn "setup reported errors; model browse/validate/run still work"
fi

# --- 4. doctor + 5. client config ---------------------------------
say "checking the install ..."
"$PY" -m invest_mcp doctor || warn "doctor flagged something (see above)"
say "Claude Code -- run once:"
"$PY" -m invest_mcp mcp-config --client claude-code
say "Claude Desktop / other -- MCP config block:"
"$PY" -m invest_mcp mcp-config
say "Full walk-through: docs/INSTALACION-PASO-A-PASO.md"
