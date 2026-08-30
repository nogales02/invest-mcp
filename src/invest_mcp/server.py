"""Build the MCPServer, register tools, run it over the configured transport.

``stdio`` (default) is for local clients that spawn the server (Claude Desktop /
Code, Cline, LibreChat, mcphost, ...). ``streamable-http`` / ``sse`` expose a
shared server other clients connect to over the network.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from invest_mcp import __version__, prompts, resources, tools
from invest_mcp.config import get_settings

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
    resources.register(server)
    prompts.register(server)
    return server


def run(transport: str | None = None, host: str | None = None,
        port: int | None = None) -> None:
    """Run the server. Args override ``INVEST_MCP_TRANSPORT`` / ``_HOST`` / ``_PORT``."""
    s = get_settings()
    transport = transport or s.transport
    server = build_server()
    if transport in ("streamable-http", "sse"):
        try:
            import uvicorn  # noqa: F401
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise SystemExit(
                f"transport '{transport}' needs uvicorn: pip install "
                '"invest-mcp[http] @ git+https://github.com/nogales02/invest-mcp"'
            ) from exc
        server.run(transport=transport, host=host or s.host, port=int(port or s.port))
    else:
        server.run(transport="stdio")


def main() -> None:
    # Kept for the console-script / `python -m invest_mcp` path; the CLI in
    # invest_mcp.cli adds `doctor` / `setup` / `mcp-config` subcommands.
    from invest_mcp.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
