"""A real MCP stdio server used by the interview demo."""

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("mini-openharness-demo")


@mcp.tool()
def word_count(text: str) -> dict[str, int]:
    """Count characters, words, and lines in text."""
    return {
        "characters": len(text),
        "words": len(text.split()),
        "lines": len(text.splitlines()),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
