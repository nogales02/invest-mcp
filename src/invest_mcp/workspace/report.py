"""Render a methods + results memo (Markdown) from what an InVEST run left on disk.

Pure stdlib. The :func:`render` function takes a plain ``payload`` dict — the
tool ``build_report`` assembles it from the job store, each job's
``datastack.json`` / ``provenance.json``, the artifact catalog and the
``summarize_results`` / ``compare_scenarios`` sidecars — and turns it into a
single Markdown document. It *collates and formats*; it never computes a
statistic or interprets a result. Convert the ``.md`` to HTML/PDF downstream
(e.g. ``pandoc report.md -o report.pdf``).
"""

from __future__ import annotations

from typing import Any


def _num(v: Any, digits: int = 4) -> str:
    if v is None or v == "":
        return "n/a"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:  # NaN
        return "n/a"
    if f == 0:
        return "0"
    if abs(f) < 1e-3 or abs(f) >= 1e7:
        return f"{f:,.{digits}g}"
    return f"{f:,.{digits}f}".rstrip("0").rstrip(".")


def _md_cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", r"\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    out = ["| " + " | ".join(_md_cell(h) for h in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(_md_cell(c) for c in r) + " |")
    return "\n".join(out)


def _bytes(n: Any) -> str:
    try:
        size = float(n)
    except (TypeError, ValueError):
        return "?"
    if size < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
def _header(p: dict) -> str:
    meta = " · ".join(
        x for x in (
            f"generated {p.get('generated_utc', '')}".strip(),
            f"invest-mcp {p['invest_mcp_version']}" if p.get("invest_mcp_version") else "",
            f"{len(p.get('jobs', []))} job(s)",
        ) if x
    )
    return f"# {p.get('title') or 'InVEST run report'}\n\n_{meta}_"


def _overview(jobs: list[dict]) -> str:
    rows = [
        [f"`{j['job_id']}`", f"`{j.get('model_id', '')}`", j.get("status", ""),
         j.get("ended_at") or j.get("created_at") or "", j.get("returncode")]
        for j in jobs
    ]
    tbl = _table(["Job", "Model", "Status", "Ended", "rc"], rows)
    return f"## Overview\n\n{tbl}" if tbl else ""


def _params_section(args: dict) -> str:
    if not args:
        return ""
    rows = [[f"`{k}`", f"`{v}`" if isinstance(v, str) else _md_cell(v)]
            for k, v in sorted(args.items())]
    return "### Parameters\n\n" + _table(["Argument", "Value"], rows)


def _provenance_section(prov: dict) -> str:
    if not prov:
        return ""
    facts = [
        f"InVEST **{prov.get('invest_version', '?')}**",
        f"invest-mcp {prov.get('invest_mcp_version', '?')}",
        f"Python {prov.get('python', '?')}",
        prov.get("platform", ""),
    ]
    lines = ["### Provenance", "", "- " + " · ".join(f for f in facts if f)]
    times = [prov.get("started_at"), prov.get("ended_at")]
    if any(times):
        lines.append(f"- Ran {times[0] or '?'} → {times[1] or '?'} "
                     f"(exit {prov.get('returncode')})")
    inputs = prov.get("inputs") or []
    if inputs:
        rows = [[f"`{i.get('arg', '')}`", f"`{i.get('path', '')}`",
                 f"`{(i.get('sha256') or '')[:16]}…`" if i.get("sha256") else "—"]
                for i in inputs]
        lines += ["", "Input files (SHA-256 at run time):", "",
                  _table(["Argument", "Path", "Digest"], rows)]
    return "\n".join(lines)


def _outputs_section(cat: dict) -> str:
    if not cat or not cat.get("exists"):
        return ""
    summ = cat.get("summary", {})
    counts = ", ".join(f"{v} {k}" for k, v in sorted(summ.items())) or "no files"
    lines = ["### Outputs", "", f"{counts}"
             + (" _(catalog truncated)_" if cat.get("truncated") else "") + "."]
    notable = [f for f in cat.get("files", [])
               if f.get("kind") in {"raster", "vector", "table", "report"}
               and "intermediate" not in f.get("path", "").lower()]
    if notable:
        rows = [[f"`{f['path']}`", f.get("kind", ""), _bytes(f.get("size_bytes"))]
                for f in notable[:40]]
        lines += ["", _table(["File", "Kind", "Size"], rows)]
        if len(notable) > 40:
            lines.append(f"\n…and {len(notable) - 40} more.")
    if cat.get("invest_logs"):
        lines.append("\nInVEST log(s): "
                     + ", ".join(f"`{p}`" for p in cat["invest_logs"]))
    return "\n".join(lines)


def _raster_stats_table(rasters: list[dict]) -> str:
    rows = []
    for r in rasters:
        if r.get("error"):
            rows.append([f"`{r.get('name', '?')}`", r.get("label") or "",
                         f"error: {r['error']}", "", "", "", ""])
            continue
        s = r.get("stats", {})
        rows.append([
            f"`{r.get('name', '?')}`",
            r.get("label") or "",
            f"{s.get('valid_count', 0):,}"
            + ("*" if s.get("approx") else ""),
            _num(s.get("mean")),
            f"{_num(s.get('min'))} – {_num(s.get('max'))}",
            _num(s.get("sum")),
            r.get("units") or "",
        ])
    return _table(["Raster", "Label", "Valid px", "Mean", "Range", "Sum", "Units"], rows)


def _aoi_bullets(aoi: dict | None, *, delta: bool = False) -> list[str]:
    if not aoi:
        return []
    if aoi.get("error"):
        return [f"- AOI zonal summary failed: {aoi['error']}"]
    feats = aoi.get("features") or []
    if not feats:
        return []
    word = "Δ by feature" if delta else "zonal summary"
    head = (f"AOI {word} — `{aoi.get('path', '')}`, {aoi.get('feature_count', len(feats))} "
            f"feature(s)" + (" (truncated)" if aoi.get("truncated") else "") + ":")
    out = [head]
    for f in feats[:15]:
        props = ", ".join(f"{k}={v}" for k, v in (f.get("properties") or {}).items())
        per = "; ".join(
            f"{rn} {'Δ ' if delta else ''}mean {_num(rs.get('mean'))}"
            + (f" (Δ sum {_num(rs.get('sum'))})" if delta and rs.get('sum') is not None else "")
            + f" over {rs.get('valid_count', 0):,} px"
            for rn, rs in (f.get("rasters") or {}).items() if rs.get("valid_count")
        )
        out.append(f"- feature {f.get('feature_index')}"
                   + (f" ({props})" if props else "") + f": {per or 'no overlap'}")
    for n in aoi.get("notes", []):
        out.append(f"- note: {n}")
    return out


def _invest_csv_table(rows: list[dict] | None) -> str:
    if not rows:
        return ""
    hdrs = list(rows[0].keys())
    return ("InVEST's own `raster_values_summary.csv`:\n\n"
            + _table(hdrs, [[r.get(h, "") for h in hdrs] for r in rows]))


def _results_section(summary: dict) -> str:
    if not summary:
        return ""
    blocks: list[str] = []
    rasters = summary.get("rasters") or []
    if rasters:
        tbl = _raster_stats_table(rasters)
        if any(r.get("stats", {}).get("approx") for r in rasters):
            tbl += "\n\n_\\* valid-pixel count from a decimated read._"
        blocks.append(tbl)
    bullets = _aoi_bullets(summary.get("aoi"))
    if bullets:
        blocks.append("\n".join(bullets))
    csv_tbl = _invest_csv_table(summary.get("invest_raster_values_summary"))
    if csv_tbl:
        blocks.append(csv_tbl)
    if not blocks:
        return ""
    return "### Results\n\n" + "\n\n".join(blocks)


def _figures_section(figs: list[dict]) -> str:
    if not figs:
        return ""
    lines = ["### Figures", ""]
    for f in figs:
        lines.append(f"![{f.get('caption') or f.get('ref', 'figure')}]({f['ref']})")
        if f.get("caption"):
            lines.append(f"\n*{f['caption']}*")
    return "\n".join(lines)


def _job_section(job: dict) -> str:
    title = job.get("model_title") or job.get("model_id") or "job"
    parts = [
        f"## {title} — `{job['job_id']}`",
        "\n".join(x for x in [
            f"- **Model:** `{job.get('model_id', '')}`",
            f"- **Status:** {job.get('status', '?')}"
            + (f" (exit {job['returncode']})" if job.get("returncode") is not None else ""),
            f"- **Ran:** {job.get('started_at') or '?'} → {job.get('ended_at') or '?'}",
            f"- **Workspace:** `{job.get('workspace', '')}`" if job.get("workspace") else "",
        ] if x),
    ]
    for section in (
        _params_section(job.get("args") or {}),
        _provenance_section(job.get("provenance") or {}),
        _outputs_section(job.get("artifacts") or {}),
        _results_section(job.get("summary") or {}),
        _figures_section(job.get("figures") or []),
    ):
        if section:
            parts.append(section)
    tail = job.get("log_tail")
    if tail and job.get("status") not in (None, "succeeded"):
        parts.append("### Run log (tail)\n\n```\n" + tail.strip() + "\n```")
    return "\n\n".join(parts)


def _comparison_section(cmp: dict) -> str:
    c = cmp.get("compare") or {}
    head = (f"## Scenario comparison — baseline `{cmp.get('baseline_job_id', '?')}` "
            f"vs scenario `{cmp.get('scenario_job_id', '?')}`")
    parts = [head]
    pairs = c.get("pairs") or []
    rows = []
    for r in pairs:
        name = (r.get("relpath") or "").split("/")[-1]
        if r.get("error"):
            rows.append([f"`{name}`", f"error: {r['error']}", "", "", "", "", "", ""])
            continue
        s = r.get("stats", {})
        bsum = s.get("overlap_baseline_sum", s.get("baseline_sum"))
        ssum = s.get("overlap_scenario_sum", s.get("scenario_sum"))
        pct = s.get("pct_change")
        rows.append([
            f"`{name}`",
            _num(bsum), _num(ssum), _num(s.get("delta_sum")),
            f"{pct:+.1f}%" if pct is not None else "n/a",
            f"{s.get('increased_px', 0):,}",
            f"{s.get('decreased_px', 0):,}",
            f"{s.get('unchanged_px', 0):,}",
        ])
    if rows:
        parts.append("Difference = scenario − baseline.\n\n" + _table(
            ["Raster", "Before", "After", "Δ", "%Δ", "px ↑", "px ↓", "px ="], rows))
    bullets = _aoi_bullets(c.get("aoi"), delta=True)
    if bullets:
        parts.append("\n".join(bullets))
    extra = []
    if c.get("only_in_baseline"):
        extra.append("Only in baseline: " + ", ".join(c["only_in_baseline"]))
    if c.get("only_in_scenario"):
        extra.append("Only in scenario: " + ", ".join(c["only_in_scenario"]))
    if extra:
        parts.append("\n".join(extra))
    fig = cmp.get("figure")
    if fig:
        parts.append(f"![{fig.get('caption') or 'difference'}]({fig['ref']})")
    return "\n\n".join(parts)


def _footer() -> str:
    return ("---\n\n_Assembled by `invest-mcp` `build_report` from each job's "
            "artifacts, `provenance.json`, and the `summarize_results` / "
            "`compare_scenarios` sidecars. Convert to HTML or PDF with e.g. "
            "`pandoc report.md -o report.pdf`._")


def render(payload: dict) -> str:
    """Turn a report payload into a Markdown document (see module docstring)."""
    blocks = [_header(payload), _overview(payload.get("jobs") or [])]
    for job in payload.get("jobs") or []:
        blocks.append(_job_section(job))
    for cmp in payload.get("comparisons") or []:
        blocks.append(_comparison_section(cmp))
    blocks.append(_footer())
    return "\n\n".join(b for b in blocks if b) + "\n"
