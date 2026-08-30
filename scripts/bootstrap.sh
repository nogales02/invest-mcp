#!/usr/bin/env bash
# One-shot installer for invest-mcp on Linux / macOS.
#
# Prereqs you install yourself: Python >= 3.10, and either the InVEST Workbench
# or a conda-forge `natcap.invest` (so an `invest` is on PATH), plus a
# conda/mamba/micromamba if you want preflight_geo / run_calibration.
#
# Usage:
#   scripts/bootstrap.sh                 # venv + install + conda envs + doctor
#   scripts/bootstrap.sh --skip-envs     # no conda envs
#   scripts/bootstrap.sh --geo           # only the invest-geo env
#   scripts/bootstrap.sh --cal           # only the invest-cal env
#   scripts/bootstrap.sh --conda /path/to/micromamba
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

say()  { printf '==> %s\n' "$*"; }
ok()   { printf '    [ok] %s\n' "$*"; }
warn() { printf '    [!!] %s\n' "$*" >&2; }
die()  { printf '    [XX] %s\n' "$*" >&2; exit 1; }

SKIP_ENVS=0; SETUP_ARGS=()
for a in "$@"; do
  case "$a" in
    --skip-envs) SKIP_ENVS=1 ;;
    --geo) SETUP_ARGS+=(--geo) ;;
    --cal) SETUP_ARGS+=(--cal) ;;
    --conda=*) SETUP_ARGS+=(--conda "${a#*=}") ;;
    *) warn "ignoring unknown arg: $a" ;;
  esac
done

say "invest-mcp bootstrap  (repo: $REPO_ROOT)"
[ -f "$REPO_ROOT/environment-geo.yml" ] || die "run this from a git clone of the repo"

# 1. python >= 3.10
if [ ! -x "$VENV_PY" ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)'; then
      PY="$c"; break
    fi
  done
  [ -n "$PY" ] || die "no Python >= 3.10 found on PATH"
  ok "using $PY"
  say "creating .venv ..."
  "$PY" -m venv "$VENV_DIR"
else
  ok ".venv already exists"
fi

# 2. install
say "installing invest-mcp into .venv ..."
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -e "$REPO_ROOT"
ok "invest-mcp installed"

# 3. conda envs
if [ "$SKIP_ENVS" -eq 1 ]; then
  warn "skipping conda envs: preflight_geo + run_calibration disabled"
else
  say "building conda envs (slow: 10-25 min) ..."
  "$VENV_PY" -m invest_mcp setup "${SETUP_ARGS[@]}" || \
    warn "setup reported errors; model browse/validate/run still works"
fi

# 4. doctor + 5. client config
say "checking the install ..."
"$VENV_PY" -m invest_mcp doctor || warn "doctor flagged something (see above)"
say "Claude Code -- run once:"
"$VENV_PY" -m invest_mcp mcp-config --client claude-code
say "Claude Desktop / other -- MCP config block:"
"$VENV_PY" -m invest_mcp mcp-config
say "Full walk-through: docs/INSTALACION-PASO-A-PASO.md"
