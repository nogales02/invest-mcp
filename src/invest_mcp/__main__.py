"""``python -m invest_mcp [serve|doctor|setup|mcp-config]`` -- no subcommand runs
the server over stdio."""

from invest_mcp.cli import main

if __name__ == "__main__":
    main()
