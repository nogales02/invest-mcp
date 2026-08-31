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


def build_template(
    key_col: str,
    columns: list[dict],
    lucodes: list[int],
    *,
    descriptions: dict[int, str] | None = None,
    include_optional: bool = True,
) -> dict:
    """Return ``{"csv", "headers", "column_help", "notes"}``.

    ``columns`` are the non-key column specs (``id``/``about``/``required``/
    ``units``). ``required`` is ``True`` (kept), ``False`` (kept only when
    ``include_optional``) or a string condition (always kept, flagged).
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
        expanded, note = expand_column(cid)
        if note:
            notes.append(note)
        requirement = (
            "required" if req is True
            else "optional" if req is False
            else f"required if: {req}"
        )
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
