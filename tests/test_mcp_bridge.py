"""The MCP bridge is served to users as a standalone script; a syntax error there breaks Claude Desktop."""
import os
import py_compile

BRIDGE = os.path.join(os.path.dirname(__file__), "..", "mcp", "anotherr_mcp.py")


def test_bridge_compiles(tmp_path):
    py_compile.compile(BRIDGE, cfile=str(tmp_path / "bridge.pyc"), doraise=True)
