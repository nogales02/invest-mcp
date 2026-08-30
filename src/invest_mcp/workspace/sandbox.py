"""Path allow-listing for InVEST input files.

The server will read whatever files a model run points at, so every input path
must resolve to a location the operator explicitly trusts: the data root, the
server's working directory, or a folder listed in
``INVEST_MCP_ALLOWED_INPUT_DIRS``. Extra folders can also be trusted for the
lifetime of the process via :func:`allow_dir`.
"""

from __future__ import annotations

from pathlib import Path

_session_allowed: list[Path] = []


class SandboxError(PermissionError):
    pass


def allow_dir(path: str | Path) -> Path:
    """Trust ``path`` (a directory) for the rest of this server process."""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise SandboxError(f"Not a directory: {p}")
    if p not in _session_allowed:
        _session_allowed.append(p)
    return p


def session_allowed() -> list[Path]:
    return list(_session_allowed)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_input_path(raw: str, allowed_roots: list[Path]) -> Path:
    if not raw or not str(raw).strip():
        raise SandboxError("Empty path.")
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError as exc:
        raise SandboxError(f"Cannot resolve path {raw!r}: {exc}") from exc
    if not p.exists():
        raise SandboxError(f"Path does not exist: {p}")

    roots = list(allowed_roots) + _session_allowed
    if any(_is_within(p, r) for r in roots):
        return p
    raise SandboxError(
        f"Path {p} is outside every allowed folder. Allowed: "
        + "; ".join(str(r) for r in roots)
        + ". Add its parent folder with the `allow_input_dir` tool, or set "
        "INVEST_MCP_ALLOWED_INPUT_DIRS and restart the server."
    )


def resolve_output_path(
    raw: str, allowed_roots: list[Path], *, allow_overwrite: bool = True
) -> Path:
    """Resolve a path the server is about to **write**.

    Unlike :func:`resolve_input_path` the target need not exist yet, but its
    location must still fall inside a trusted folder (the data root, the working
    directory, a configured/allowed input dir, or a folder trusted this session
    via :func:`allow_dir` -- e.g. a scaffolded project). The parent directory is
    *not* created here; the worker does that.
    """
    if not raw or not str(raw).strip():
        raise SandboxError("Empty path.")
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError as exc:
        raise SandboxError(f"Cannot resolve path {raw!r}: {exc}") from exc

    roots = list(allowed_roots) + _session_allowed
    if not any(_is_within(p, r) for r in roots):
        raise SandboxError(
            f"Output path {p} is outside every allowed folder. Allowed: "
            + "; ".join(str(r) for r in roots)
            + ". Scaffold a project under one of them, or trust its parent "
            "folder with the `allow_input_dir` tool."
        )
    if p.is_dir():
        raise SandboxError(f"Output path is an existing directory: {p}")
    if p.exists() and not allow_overwrite:
        raise SandboxError(f"Refusing to overwrite existing file: {p}")
    return p


def check_input_paths(
    args: dict, path_arg_types: dict[str, str], allowed_roots: list[Path]
) -> dict[str, str]:
    """Validate every path-typed arg present in ``args``.

    Returns ``{arg_name: resolved_absolute_path}`` for the ones that were set.
    Raises :class:`SandboxError` on the first offending path.
    """
    resolved: dict[str, str] = {}
    for name in path_arg_types:
        value = args.get(name)
        if value in (None, "", []):
            continue
        resolved[name] = str(resolve_input_path(str(value), allowed_roots))
    return resolved
