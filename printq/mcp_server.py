"""Model Context Protocol server (stdio) exposing the printq tools.

Run as `python -m printq.mcp_server`. Any MCP client can drive the queue
through it; the gates live in tools.py, not in the client.

Set PRINTQ_MCP_TOOLS to a comma-separated list to expose only some tools
(the queue gives the prepare and launch phases different subsets).
"""
from __future__ import annotations

import json
import os
import sys

from . import tools

PROTOCOL = "2024-11-05"


def _tool_list() -> list[dict]:
    allowed = [n.strip() for n in os.environ.get("PRINTQ_MCP_TOOLS", "").split(",") if n.strip()]
    return [{"name": name, "description": " ".join((fn.__doc__ or "").split()), "inputSchema": schema}
            for name, (fn, schema) in tools.REGISTRY.items() if not allowed or name in allowed]


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "printq", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": _tool_list()}
    elif method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name", "")
        if name not in {t["name"] for t in _tool_list()}:
            out = {"refused": f"tool '{name}' is not available in this phase"}
        else:
            out = tools.call(name, params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": json.dumps(out)}], "isError": "refused" in out}
    elif method == "ping":
        result = {}
    elif mid is None:
        return None  # a notification; nothing to answer
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001 - never let one bad message kill the server
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
