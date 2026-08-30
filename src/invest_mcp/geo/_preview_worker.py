"""Standalone raster-preview renderer -- RUN AS ITS OWN PROCESS.

matplotlib's Agg backend segfaults on ``savefig`` on some Windows machines (a
native-DLL clash, not a Python error -- see CLAUDE.md). Rendering the preview in
a throw-away subprocess means that crash costs us only the PNG, never the summary.

Usage::

    <python> -m invest_mcp.geo._preview_worker <raster_path> <out_png> [label] [--diverging]

``--diverging`` centres the colour scale on zero with a red/blue map -- for a
``scenario - baseline`` difference raster (see :mod:`invest_mcp.geo.compare`).

Exit 0 and the PNG exists  -> success.
Anything else              -> caller falls back to "no preview".
"""

from __future__ import annotations

import sys


def _render(raster_path: str, out_png: str, label: str, diverging: bool = False) -> int:
    import numpy as np
    import rasterio

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with rasterio.open(raster_path) as ds:
        # decimate so the read (and the figure) stay small regardless of extent
        longest = max(ds.width, ds.height)
        scale = max(1, int(np.ceil(longest / 900)))
        out_h, out_w = ds.height // scale, ds.width // scale
        band = ds.read(1, out_shape=(out_h, out_w), masked=True)
        nodata = ds.nodata

    arr = np.ma.masked_invalid(band.astype("float64"))
    if nodata is not None:
        arr = np.ma.masked_equal(arr, float(nodata))

    finite = arr.compressed()
    if finite.size:
        vmin, vmax = np.percentile(finite, [2, 98])
        if vmin == vmax:
            vmin, vmax = float(finite.min()), float(finite.max())
    else:
        vmin, vmax = 0.0, 1.0

    if diverging:
        lim = max(abs(float(vmin)), abs(float(vmax))) or 1.0
        vmin, vmax, cmap = -lim, lim, "RdBu_r"
    else:
        cmap = "viridis"

    fig, ax = plt.subplots(figsize=(7, 6), dpi=110)
    im = ax.imshow(arr, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
    ax.set_title(label or raster_path, fontsize=10)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.colorbar(im, ax=ax, shrink=0.85)
    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write(
            "usage: _preview_worker <raster_path> <out_png> [label] [--diverging]\n")
        return 2
    raster_path, out_png = argv[0], argv[1]
    rest = argv[2:]
    diverging = "--diverging" in rest
    labels = [a for a in rest if a != "--diverging"]
    label = labels[0] if labels else ""
    try:
        return _render(raster_path, out_png, label, diverging)
    except Exception as exc:  # noqa: BLE001 - this whole process is best-effort
        sys.stderr.write(f"preview render failed: {type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
