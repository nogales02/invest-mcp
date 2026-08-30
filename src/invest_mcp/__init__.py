"""invest-mcp: an MCP server that drives Natural Capital Project InVEST models.

Architecture (v0.1):

    MCP client (Claude Code)
        |  stdio / JSON-RPC
        v
    invest_mcp.server  (MCPServer, tool registration)
        |
        +-- models/      discover models, translate MODEL_SPEC -> JSON Schema
        +-- execution/   job store + subprocess runner around `invest run`
        +-- workspace/   path sandbox + output artifact catalog
        +-- provenance   per-run reproducibility manifest
        |
        v
    invest.exe  (bundled with the InVEST Workbench)  -- does the actual compute
"""

__version__ = "0.1.0"
