"""Entry point: build the MCPServer, register tools, run over stdio."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from invest_mcp import __version__, tools

INSTRUCTIONS = """\
Drive Natural Capital Project InVEST models.

Typical flow:
  1. invest_env()                       -> confirm InVEST is reachable
  2. list_invest_models()               -> pick a model_id
  3. describe_invest_model(model_id)    -> get args_schema + required inputs
  4. validate_invest_args(model_id, args)   (also runs the geospatial preflight)
     preflight_geo(model_id, args)          -> deep CRS/overlap/pixel check on its own
  5. run_invest_model(model_id, args)   -> returns job_id
  6. get_invest_job(job_id)             -> poll until status is terminal
  7. list_invest_job_artifacts(job_id)  -> inspect outputs

`args` is InVEST's own args dict (paths must be absolute). Do not set
`workspace_dir`; the server manages it. Input files must live under an allowed
folder -- use allow_input_dir(path) if a path is rejected.
"""


def build_server() -> MCPServer:
    server = MCPServer(
        name="invest",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    tools.register(server)
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
