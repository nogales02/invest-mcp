"""Biophysical / lookup table skeletons -- pure, no GDAL.

Given a model's column spec (from :func:`invest_mcp.models.spec_translate.table_arg_specs`)
and the list of land-cover codes in the LULC raster (scanned in the ``invest-geo``
env), emit a CSV with one row per code and the header columns InVEST expects,
values left blank for the user to fill. The MCP fills structure, not numbers --
the coefficients are the user's (or a cited knowledge base's) call.
"""

from __future__ import annotations

import csv
import io

# Column-id placeholders with a fixed, safe vocabulary we can expand for the user.
_COLUMN_EXPANSIONS: dict[str, list[str]] = {
    "[MONTH]": [str(m) for m in range(1, 13)],
    "[SOIL_GROUP]": ["a", "b", "c", "d"],
}


def expand_column(col_id: str) -> tuple[list[str], str | None]:
    """``"kc_[MONTH]"`` -> ``(["kc_1", ..., "kc_12"], None)``.

    A ``[TOKEN]`` we don't know is left as one literal header plus a note telling
    the user to expand it against their own data.
    """
    for token, values in _COLUMN_EXPANSIONS.items():
        if token in col_id:
            return [col_id.replace(token, v) for v in values], None
    if "[" in col_id and "]" in col_id:
        tok = col_id[col_id.index("["): col_id.index("]") + 1]
        return [col_id], (
            f"{col_id}: expand {tok} to one column per "
            f"{tok.strip('[]').lower()} value in your data"
        )
    return [col_id], None


def _requirement_label(
    req: object, conditions: dict[str, bool] | None
) -> str | None:
    """The requirement tag for a column, given its spec ``required`` value and a
    resolved-conditions map (``{condition: on/off}``). ``None`` means *drop this
    column* -- a string condition explicitly resolved ``False``.

    ``req`` is ``True`` (always required), ``False`` (optional) or a string (a
    condition, e.g. NDR's ``"calc_n"``). With ``conditions`` a string condition
    known ``True`` becomes a hard ``"required"``, known ``False`` drops the
    column, and an unlisted condition stays ``"required if: <cond>"``.
    """
    if req is True:
        return "required"
    if req is False:
        return "optional"
    cond = conditions.get(req) if (conditions is not None and isinstance(req, str)) else None
    if cond is True:
        return "required"
    if cond is False:
        return None
    return f"required if: {req}"


def build_template(
    key_col: str,
    columns: list[dict],
    lucodes: list[int],
    *,
    descriptions: dict[int, str] | None = None,
    include_optional: bool = True,
    conditions: dict[str, bool] | None = None,
) -> dict:
    """Return ``{"csv", "headers", "column_help", "notes"}``.

    ``columns`` are the non-key column specs (``id``/``about``/``required``/
    ``units``). ``required`` is ``True`` (kept), ``False`` (kept only when
    ``include_optional``) or a string condition (always kept, flagged).

    ``conditions`` (``{condition: bool}``, e.g. ``{"calc_n": True, "calc_p":
    False}``) resolves those string conditions: a condition known ``True`` turns
    its columns into hard ``required`` ones, known ``False`` drops them, and an
    unlisted condition is left ``required if: <cond>`` as before.
    """
    headers = [key_col]
    column_help: dict[str, dict] = {}
    notes: list[str] = []
    seen = {key_col}

    for c in columns:
        cid = c["id"]
        if cid == key_col or cid in seen:
            continue
        req = c["required"]
        if req is False and not include_optional:
            continue
        requirement = _requirement_label(req, conditions)
        if requirement is None:              # conditional branch resolved off
            continue
        expanded, note = expand_column(cid)
        if note:
            notes.append(note)
        for h in expanded:
            if h in seen:
                continue
            seen.add(h)
            headers.append(h)
            column_help[h] = {
                "about": c["about"],
                "units": c["units"],
                "requirement": requirement,
                "expanded_from": cid if h != cid else None,
            }

    add_desc = descriptions is not None and "description" not in seen
    out_headers = headers + (["description"] if add_desc else [])

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(out_headers)
    for code in lucodes:
        row: list = [code] + [""] * (len(headers) - 1)
        if add_desc:
            row.append((descriptions or {}).get(code, ""))
        w.writerow(row)

    return {
        "csv": buf.getvalue(),
        "headers": out_headers,
        "column_help": column_help,
        "notes": notes,
    }


def _num(cell: object) -> float | None:
    """A CSV cell as a float, or ``None`` if blank / not a number."""
    s = str(cell).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_table(text: str) -> tuple[list[str], list[dict]]:
    """CSV text -> ``(headers, [row-dict])``.

    Header cells are stripped; short rows are padded, long rows keep only the
    first ``len(headers)`` cells. Fully blank lines are dropped. Read the file
    with ``utf-8-sig`` so a BOM does not end up in the first header.
    """
    rows = [r for r in csv.reader(io.StringIO(text)) if any(str(c).strip() for c in r)]
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    out: list[dict] = []
    for r in rows[1:]:
        cells = list(r) + [""] * (len(headers) - len(r))
        out.append({h: cells[i] for i, h in enumerate(headers)})
    return headers, out


# Hard invariants -- true no matter which citation a value came from.
_FRACTION_COLS = {"usle_c", "usle_p", "eff_n", "eff_p", "proportion_subsurface_n"}
_NONNEG_COLS = {
    "load_n", "load_p", "crit_len_n", "crit_len_p", "root_depth",
    "c_above", "c_below", "c_soil", "c_dead",
}
_CN_QUAD = ("cn_a", "cn_b", "cn_c", "cn_d")
_KNOWN_EXTRA = {"description", "lucode_name", "lulc_name", "name"}


def _numeric_col(hl: str, ranges: dict) -> bool:
    return hl in ranges or hl in _FRACTION_COLS or hl in _NONNEG_COLS or hl in _CN_QUAD


def check_table(
    key_col: str,
    columns: list[dict],
    table_text: str,
    *,
    raster_codes: list[int] | None = None,
    column_ranges: dict | None = None,
    include_optional: bool = True,
    conditions: dict[str, bool] | None = None,
) -> dict:
    """Check a filled biophysical / lookup CSV against a model's column spec, the
    land-cover raster (optional) and the cited-coefficient typical ranges.

    ``columns`` are the non-key column specs from
    :func:`invest_mcp.models.spec_translate.table_arg_specs` (``[TOKEN]``
    placeholders still un-expanded). ``column_ranges`` is
    :func:`invest_mcp.knowledge.coefficients.column_ranges` output.

    ``conditions`` (``{condition: bool}``, e.g. ``{"calc_n": True}``) resolves
    the models's string ``required`` conditions: a condition known ``True``
    promotes its columns to hard-required (absent -> ``missing``, blank ->
    ``empty_required``); known ``False`` means that branch is off, so the
    columns are neither required nor flagged as unexpected; an unlisted
    condition stays advisory (listed under ``conditional_columns``).

    Returns ``{"severity", "pass", "checks", "enforced_conditions", ...}`` --
    ``severity`` is ``error`` (a blocker: missing rows/columns, empty required
    cell, non-numeric coefficient, invariant broken), ``warning``
    (orphan/duplicate rows, unexpected columns, a value outside the cited
    literature band) or ``ok``.
    """
    ranges = column_ranges or {}
    conds = conditions or {}
    headers, rows = read_table(table_text)
    lc = {h.lower(): h for h in headers}
    key_hdr = lc.get(key_col.lower(), key_col)

    # -- expected columns from the spec --
    expected: list[tuple[str, str]] = []
    branch_off: set[str] = set()          # cols whose condition is resolved False
    for c in columns:
        cid = c["id"]
        if cid == key_col:
            continue
        req = c["required"]
        if req is False and not include_optional:
            continue
        requirement = _requirement_label(req, conds)
        if requirement is None:           # conditional branch resolved off
            branch_off.update(h.lower() for h in expand_column(cid)[0])
            continue
        for h in expand_column(cid)[0]:
            expected.append((h, requirement))
    exp_names = {h.lower() for h, _ in expected}
    required_names = {h.lower() for h, r in expected if r == "required"}
    conditional = sorted({h for h, r in expected if r.startswith("required if")})
    enforced_conditions = sorted(
        {c["required"] for c in columns
         if isinstance(c["required"], str) and conds.get(c["required"]) is True}
    )

    missing_cols = sorted(h for h, r in expected if r == "required" and h.lower() not in lc)
    skip = {key_col.lower()} | _KNOWN_EXTRA | branch_off
    unexpected_cols = sorted(
        h for h in headers if h.lower() not in exp_names and h.lower() not in skip
    )

    # -- coverage against the raster --
    coverage: dict = {}
    if raster_codes is not None:
        seen: set[int] = set()
        dups: set[int] = set()
        for row in rows:
            v = _num(row.get(key_hdr, ""))
            if v is None:
                continue
            iv = int(v)
            if iv in seen:
                dups.add(iv)
            seen.add(iv)
        rc = {int(x) for x in raster_codes}
        coverage = {
            "missing_rows": sorted(rc - seen),
            "orphan_rows": sorted(seen - rc),
            "duplicate_rows": sorted(dups),
        }

    # -- cells + ranges + invariants --
    empty_required: list[dict] = []
    non_numeric: list[dict] = []
    out_of_typical: list[dict] = []
    invariant: list[dict] = []

    # cell / range / invariant checks only touch columns this model actually
    # consumes -- an unexpected extra column is reported once, above, and then
    # left alone (InVEST ignores it; its values do not affect this run).
    checkable = exp_names

    for row in rows:
        rid = (str(row.get(key_hdr, "")).strip() or "?")
        vals = {h.lower(): _num(row.get(h, "")) for h in headers if h.lower() in checkable}
        for h in headers:
            hl = h.lower()
            if hl in skip or hl not in checkable:
                continue
            raw = str(row.get(h, "")).strip()
            if hl in required_names and raw == "":
                empty_required.append({"row": rid, "column": h})
                continue
            if raw == "":
                continue
            v = vals.get(hl)
            if v is None:
                if _numeric_col(hl, ranges):
                    non_numeric.append({"row": rid, "column": h, "value": raw})
                continue
            if hl in _FRACTION_COLS and not 0.0 <= v <= 1.0:
                invariant.append({"row": rid, "column": h, "value": v,
                                  "rule": f"{h} must be a fraction in [0, 1]"})
            if hl in _NONNEG_COLS and v < 0:
                invariant.append({"row": rid, "column": h, "value": v,
                                  "rule": f"{h} must be >= 0"})
            if hl in _CN_QUAD and not 0 < v <= 100:
                invariant.append({"row": rid, "column": h, "value": v,
                                  "rule": f"{h} must be in (0, 100]"})
            if hl == "root_depth" and v != int(v):
                invariant.append({"row": rid, "column": h, "value": v,
                                  "rule": "root_depth should be an integer (mm)"})
            band = ranges.get(hl, {}).get("typical")
            if band and not band[0] <= v <= band[1]:
                out_of_typical.append({"row": rid, "column": h, "value": v,
                                       "typical": band, "resource": ranges[hl]["resource"]})
        quad = [vals.get(k) for k in _CN_QUAD]
        if all(isinstance(x, float) for x in quad) and quad != sorted(quad):
            invariant.append({
                "row": rid, "columns": [k.upper() for k in _CN_QUAD],
                "values": {k.upper(): vals.get(k) for k in _CN_QUAD},
                "rule": "curve numbers must be ordered CN_A <= CN_B <= CN_C <= CN_D",
            })

    has_error = bool(
        missing_cols or empty_required or non_numeric or invariant
        or coverage.get("missing_rows")
    )
    has_warning = bool(
        unexpected_cols or out_of_typical
        or coverage.get("orphan_rows") or coverage.get("duplicate_rows")
    )
    severity = "error" if has_error else "warning" if has_warning else "ok"

    return {
        "severity": severity,
        "pass": not has_error,
        "headers": headers,
        "row_count": len(rows),
        "conditional_columns": conditional,
        "enforced_conditions": enforced_conditions,
        "checks": {
            "coverage": coverage,
            "columns": {"missing": missing_cols, "unexpected": unexpected_cols},
            "cells": {"empty_required": empty_required, "non_numeric": non_numeric},
            "ranges": {
                "out_of_typical": out_of_typical,
                "invariant_violations": invariant,
            },
        },
    }


def parse_legend(text: str) -> dict[int, str]:
    """First two columns of a CSV -> ``{int code: label}``. A non-numeric first
    cell (a header row) is skipped."""
    out: dict[int, str] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        try:
            code = int(str(row[0]).strip())
        except ValueError:
            continue
        out[code] = str(row[1]).strip()
    return out
