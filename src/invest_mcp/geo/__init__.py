"""Geospatial helpers.

Everything under this package that touches GDAL runs in the separate ``invest-geo``
conda environment (see ``environment.yml``). The MCP server process itself only
imports :mod:`invest_mcp.geo.client`, which shells out to that env.
"""
