"""
MCP Tool Server for Fla.

JSON-RPC 2.0 over stdio with Content-Length framing (LSP-style, per MCP spec).

Thread safety: A lock serializes access to self.repo, matching the REST server
pattern. Currently MCP runs over stdio (single-client), but this protects
against future concurrent request handling.
"""

import json
import logging
import sys
import threading
from pathlib import Path

from .repo import Repository
from .state import AgentIdentity

logger = logging.getLogger(__name__)


class MCPServer:
    """MCP tool server that exposes Fla operations as tools."""

    def __init__(self, repo_path: Path):
        self.repo = Repository.find(Path(repo_path))
        self._repo_lock = threading.Lock()

    def _define_tools(self) -> list:
        """Return all tool definitions with JSON Schema input schemas."""
        return [
            {
                "name": "fla_status",
                "description": "Get repository status",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "fla_snapshot",
                "description": "Snapshot a workspace",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string", "description": "Workspace name"},
                    },
                },
            },
            {
                "name": "fla_commit",
                "description": "Quick commit: snapshot + propose + accept",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "Commit message"},
                        "agent_id": {"type": "string", "description": "Agent identifier"},
                        "agent_type": {"type": "string", "description": "Agent type"},
                    },
                    "required": ["prompt", "agent_id", "agent_type"],
                },
            },
            {
                "name": "fla_history",
                "description": "Get transition history",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lane": {"type": "string", "description": "Lane name"},
                        "limit": {"type": "integer", "description": "Max results"},
                    },
                },
            },
            {
                "name": "fla_diff",
                "description": "Diff two states",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "state_a": {"type": "string"},
                        "state_b": {"type": "string"},
                    },
                    "required": ["state_a", "state_b"],
                },
            },
            {
                "name": "fla_show",
                "description": "Show file content at a state",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "state_id": {"type": "string"},
                        "file_path": {"type": "string"},
                    },
                    "required": ["state_id", "file_path"],
                },
            },
            {
                "name": "fla_search",
                "description": "Search intents",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fla_lanes",
                "description": "List lanes",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "fla_workspaces",
                "description": "List workspaces",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "fla_accept",
                "description": "Accept a transition",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "transition_id": {"type": "string"},
                    },
                    "required": ["transition_id"],
                },
            },
            {
                "name": "fla_reject",
                "description": "Reject a transition",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "transition_id": {"type": "string"},
                    },
                    "required": ["transition_id"],
                },
            },
            {
                "name": "fla_restore",
                "description": "Restore a workspace to a state",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "state_id": {"type": "string"},
                    },
                    "required": ["workspace", "state_id"],
                },
            },
        ]

    def handle_request(self, request: dict) -> dict | None:
        """Handle a JSON-RPC 2.0 request. Returns response dict or None for notifications."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        # Per JSON-RPC 2.0: notifications (requests without "id") get no response
        if "id" not in request:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {},
                    },
                    "serverInfo": {
                        "name": "fla-mcp",
                        "version": "0.3.0",
                    },
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": self._define_tools(),
                },
            }

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            try:
                with self._repo_lock:
                    result = self._call_tool(tool_name, tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                    },
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                        "isError": True,
                    },
                }

        # Unknown method
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }

    def _call_tool(self, name: str, args: dict) -> dict | list:
        """Dispatch tool call to the appropriate repo method."""
        if name == "fla_status":
            return self.repo.status()

        elif name == "fla_snapshot":
            workspace = args.get("workspace", "main")
            state_id = self.repo.snapshot(workspace)
            return {"state_id": state_id}

        elif name == "fla_commit":
            agent = AgentIdentity(
                agent_id=args["agent_id"],
                agent_type=args["agent_type"],
            )
            return self.repo.quick_commit(
                workspace=args.get("workspace", "main"),
                prompt=args["prompt"],
                agent=agent,
                auto_accept=True,
            )

        elif name == "fla_history":
            return self.repo.history(
                lane=args.get("lane"),
                limit=args.get("limit", 50),
            )

        elif name == "fla_diff":
            return self.repo.diff(args["state_a"], args["state_b"])

        elif name == "fla_show":
            state = self.repo.wsm.get_state(args["state_id"])
            if not state:
                raise ValueError(f"State not found: {args['state_id']}")
            files = self.repo.wsm._flatten_tree(state["root_tree"])
            blob_hash = files.get(args["file_path"])
            if not blob_hash:
                raise ValueError(f"File not found: {args['file_path']}")
            obj = self.repo.store.retrieve(blob_hash)
            if not obj:
                raise ValueError(f"Blob not found: {blob_hash}")
            import base64

            return {
                "path": args["file_path"],
                "blob_hash": blob_hash,
                "size": obj.size,
                "content_base64": base64.b64encode(obj.data).decode("ascii"),
            }

        elif name == "fla_search":
            return self.repo.search(args["query"])

        elif name == "fla_lanes":
            return self.repo.lanes()

        elif name == "fla_workspaces":
            ws_list = self.repo.workspaces()
            return [w.to_dict() for w in ws_list]

        elif name == "fla_accept":
            status = self.repo.accept(args["transition_id"])
            return {"status": status.value}

        elif name == "fla_reject":
            status = self.repo.reject(args["transition_id"])
            return {"status": status.value}

        elif name == "fla_restore":
            result = self.repo.restore(args["workspace"], args["state_id"])
            return result

        else:
            raise ValueError(f"Unknown tool: {name}")

    def _read_message(self) -> str | None:
        """Read a Content-Length framed message from stdin."""
        headers = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None  # EOF
            line = line.decode("utf-8").strip()
            if not line:
                break  # Empty line separates headers from body
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip()] = value.strip()

        content_length = int(headers.get("Content-Length", 0))
        if content_length == 0:
            return ""  # Empty body, not EOF

        body = sys.stdin.buffer.read(content_length)
        return body.decode("utf-8")

    def _write_message(self, response: dict):
        """Write a Content-Length framed message to stdout."""
        body = json.dumps(response).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n"
        sys.stdout.buffer.write(header.encode("utf-8"))
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()

    def run(self):
        """Main loop: read stdin, dispatch, write stdout."""
        # On Windows, stdin/stdout default to text mode which corrupts binary framing
        import os

        if os.name == "nt":
            import msvcrt

            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        try:
            while True:
                message = self._read_message()
                if message is None:
                    break  # EOF
                if not message.strip():
                    continue  # Empty body, skip
                try:
                    request = json.loads(message)
                except json.JSONDecodeError:
                    self._write_message(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        }
                    )
                    continue

                response = self.handle_request(request)
                if response is not None:
                    self._write_message(response)
        finally:
            self.repo.close()


def run_mcp_server(repo_path: Path):
    """Entry point for the MCP server."""
    server = MCPServer(repo_path)
    server.run()
