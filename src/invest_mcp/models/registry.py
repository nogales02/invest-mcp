"""Model discovery and MODEL_SPEC caching.

The list of models and each spec are stable for a given InVEST install, so we
cache them in-process. Call :func:`refresh` to drop the caches (e.g. after the
user points the server at a different InVEST version).
"""

from __future__ import annotations

from functools import lru_cache

from invest_mcp import invest_cli
from invest_mcp.invest_cli import ModelInfo


class UnknownModelError(KeyError):
    pass


@lru_cache(maxsize=1)
def _models() -> tuple[ModelInfo, ...]:
    return tuple(invest_cli.list_models())


@lru_cache(maxsize=None)
def get_spec(model_id: str) -> dict:
    return invest_cli.getspec(model_id)


def list_models() -> list[ModelInfo]:
    return list(_models())


def _alias_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for info in _models():
        mapping[info.model_id] = info.model_id
        for alias in info.aliases:
            mapping[alias] = info.model_id
    return mapping


def resolve_model_id(name_or_alias: str) -> str:
    """Accept a canonical id or any documented alias; return the canonical id."""
    key = (name_or_alias or "").strip()
    mapping = _alias_map()
    if key in mapping:
        return mapping[key]
    lowered = {k.lower(): v for k, v in mapping.items()}
    if key.lower() in lowered:
        return lowered[key.lower()]
    raise UnknownModelError(
        f"Unknown InVEST model {name_or_alias!r}. Known ids: "
        + ", ".join(sorted(m.model_id for m in _models()))
    )


def refresh() -> None:
    _models.cache_clear()
    get_spec.cache_clear()
