"""Runtime configuration for the InVEST MCP server.

Everything is overridable through ``INVEST_MCP_*`` environment variables (or a
``.env`` file in the working directory). Defaults are chosen so the server works
out of the box on a machine that has the InVEST Workbench installed.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Where installed InVEST Workbenches keep their bundled CLI, relative to the
# install root. Newer builds ship ``resources/invest/invest.exe``.
_WORKBENCH_GLOBS = (
    r"C:\Program Files\InVEST *Workbench\resources\invest\invest.exe",
    r"C:\Program Files (x86)\InVEST *Workbench\resources\invest\invest.exe",
)


def _version_key(path: Path) -> tuple:
    """Sort key that orders 'InVEST 3.20.1 Workbench' above 'InVEST 3.9 Workbench'."""
    m = re.search(r"InVEST\s+([\d.]+)", str(path))
    if not m:
        return (0,)
    return tuple(int(p) for p in m.group(1).split(".") if p.isdigit())


def detect_invest_exe() -> Path | None:
    """Best effort discovery of an ``invest`` executable."""
    # 1. Explicit override.
    env = os.environ.get("INVEST_MCP_INVEST_EXE")
    if env and Path(env).is_file():
        return Path(env)

    # 2. Bundled with an installed Workbench (pick the highest version).
    candidates: list[Path] = []
    for pattern in _WORKBENCH_GLOBS:
        base = Path(pattern).anchor
        rest = pattern[len(base):]
        try:
            candidates.extend(p for p in Path(base).glob(rest) if p.is_file())
        except OSError:
            continue
    if candidates:
        return sorted(candidates, key=_version_key)[-1]

    # 3. On PATH.
    from shutil import which

    for name in ("invest", "invest.exe"):
        found = which(name)
        if found:
            return Path(found)
    return None


_GEO_ENV_NAME = "invest-geo"
_CAL_ENV_NAME = "invest-cal"


def _conda_env_roots() -> list[Path]:
    roots: list[Path] = []
    for var in ("CONDA_ROOT", "CONDA_PREFIX", "MAMBA_ROOT_PREFIX"):
        val = os.environ.get(var)
        if val:
            roots.append(Path(val))
    roots += [
        Path.home() / ".conda",
        Path.home() / "miniconda3",
        Path.home() / "anaconda3",
        Path(r"C:\ProgramData\miniconda3"),
        Path(r"C:\ProgramData\anaconda3"),
    ]
    return roots


def _detect_env_python(env_name: str, override_var: str) -> Path | None:
    env = os.environ.get(override_var)
    if env and Path(env).is_file():
        return Path(env)
    seen: set[Path] = set()
    for root in _conda_env_roots():
        for cand in (
            root / "envs" / env_name / "python.exe",
            root / "envs" / env_name / "bin" / "python",
        ):
            if cand in seen:
                continue
            seen.add(cand)
            if cand.is_file():
                return cand
    return None


def detect_geo_python() -> Path | None:
    """Locate the python.exe of the ``invest-geo`` conda environment."""
    return _detect_env_python(_GEO_ENV_NAME, "INVEST_MCP_GEO_PYTHON")


def detect_cal_python() -> Path | None:
    """Locate the python.exe of the ``invest-cal`` conda environment
    (natcap.invest + spotpy, used for model calibration)."""
    return _detect_env_python(_CAL_ENV_NAME, "INVEST_MCP_CAL_PYTHON")


def geo_subprocess_env(geo_python: Path) -> dict[str, str]:
    """Environment for running the geo worker: point GDAL/PROJ at the conda env's
    data dirs and put its DLL folder on PATH."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    prefix = geo_python.parent  # <env> on Windows, <env>/bin on POSIX
    if prefix.name == "bin":
        prefix = prefix.parent
    gdal_data = prefix / "Library" / "share" / "gdal"
    proj_data = prefix / "Library" / "share" / "proj"
    if not gdal_data.is_dir():  # POSIX layout
        gdal_data = prefix / "share" / "gdal"
        proj_data = prefix / "share" / "proj"
    if gdal_data.is_dir():
        env["GDAL_DATA"] = str(gdal_data)
    if proj_data.is_dir():
        env["PROJ_DATA"] = str(proj_data)
        env["PROJ_LIB"] = str(proj_data)
    libbin = prefix / "Library" / "bin"
    if libbin.is_dir():
        env["PATH"] = str(libbin) + os.pathsep + env.get("PATH", "")
    return env


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVEST_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    invest_exe: Path | None = Field(default=None)
    geo_python: Path | None = Field(default=None)
    cal_python: Path | None = Field(default=None)
    data_root: Path = Field(default_factory=lambda: Path.home() / "invest-mcp-data")
    allowed_input_dirs: list[Path] = Field(default_factory=list)
    max_concurrent_jobs: int = Field(default=2, ge=1, le=32)
    invest_timeout_seconds: int = Field(default=6 * 60 * 60, ge=60)
    locale: str | None = Field(default=None)

    @field_validator("allowed_input_dirs", mode="before")
    @classmethod
    def _split_paths(cls, v):
        if isinstance(v, str):
            parts = re.split(r"[;:]" if os.name == "nt" else r":", v)
            return [p for p in (s.strip() for s in parts) if p]
        return v

    # ----- derived helpers -------------------------------------------------
    @property
    def jobs_dir(self) -> Path:
        return self.data_root / "jobs"

    @property
    def resolved_invest_exe(self) -> Path:
        exe = self.invest_exe or detect_invest_exe()
        if exe is None:
            raise RuntimeError(
                "Could not find an 'invest' executable. Install the InVEST "
                "Workbench, or set INVEST_MCP_INVEST_EXE to the full path of "
                "invest.exe."
            )
        return Path(exe)

    @property
    def resolved_geo_python(self) -> Path:
        p = self.geo_python or detect_geo_python()
        if p is None:
            raise RuntimeError(
                "The 'invest-geo' conda environment was not found. Create it with "
                "`conda env create -f environment.yml` (or "
                "`conda create -n invest-geo -c conda-forge --override-channels "
                "python=3.12 gdal rasterio pyproj shapely pyogrio numpy "
                "pygeoprocessing pip`), then `pip install -e . --no-deps` into it. "
                "Or set INVEST_MCP_GEO_PYTHON to a python.exe that has "
                "rasterio + pyproj + shapely + pyogrio."
            )
        return Path(p)

    @property
    def resolved_cal_python(self) -> Path:
        p = self.cal_python or detect_cal_python()
        if p is None:
            raise RuntimeError(
                "The 'invest-cal' conda environment was not found. Create it with "
                "`conda create -n invest-cal -c conda-forge --override-channels "
                "python=3.12 natcap.invest geopandas rasterstats matplotlib-base "
                "openpyxl rasterio pyogrio shapely pyproj pygeoprocessing pandas "
                "numpy pip`, then `pip install spotpy` and "
                "`pip install invest-calibration-assistant` into it. "
                "Or set INVEST_MCP_CAL_PYTHON."
            )
        return Path(p)

    def allowed_roots(self, extra: list[Path] | None = None) -> list[Path]:
        roots = [self.data_root, Path.cwd(), *self.allowed_input_dirs, *(extra or [])]
        out: list[Path] = []
        for r in roots:
            try:
                out.append(Path(r).resolve())
            except OSError:
                continue
        return out

    def ensure_dirs(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
