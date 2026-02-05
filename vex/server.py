"""
REST API Server for Vex.

Uses stdlib http.server with ThreadingHTTPServer for concurrent request handling.
Each request acquires a repo lock to serialize SQLite access safely.
"""

import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .repo import Repository
from .state import AgentIdentity

logger = logging.getLogger(__name__)


class VexHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Vex REST API."""

    @property
    def repo(self) -> Repository:
        return self.server.repo

    @property
    def repo_lock(self) -> threading.Lock:
        return self.server._repo_lock

    def log_message(self, format, *args):
        """Route HTTP request logging through the standard logging module."""
        logger.debug(format, *args)

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status=status)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _parse_path(self) -> tuple:
        """Parse request path and query parameters."""
        parsed = urlparse(self.path)
        params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}
        return parsed.path.rstrip("/"), params

    def do_GET(self):
        path, params = self._parse_path()

        try:
            # Health endpoint doesn't need repo lock
            if path == "/health":
                self._send_json({
                    "status": "healthy",
                    "version": "0.3.0",
                })
                return

            # All other endpoints need repo lock for thread safety
            with self.repo_lock:
                if path == "/status":
                    self._send_json(self.repo.status())

                elif path == "/head":
                    lane = params.get("lane")
                    head = self.repo.head(lane)
                    self._send_json({"head": head, "lane": lane or self.repo._default_lane()})

                elif path == "/lanes":
                    self._send_json(self.repo.lanes())

                elif path == "/history":
                    lane = params.get("lane")
                    limit = int(params.get("limit", 50))
                    status = params.get("status")
                    self._send_json(self.repo.history(lane=lane, limit=limit, status=status))

                elif path.startswith("/states/"):
                    rest = path[len("/states/"):]
                    if "/files/" in rest:
                        # /states/<id>/files/<path>
                        state_id, file_path = rest.split("/files/", 1)
                        state = self.repo.wsm.get_state(state_id)
                        if not state:
                            self._send_error(404, f"State not found: {state_id}")
                            return
                        files = self.repo.wsm._flatten_tree(state["root_tree"])
                        blob_hash = files.get(file_path)
                        if not blob_hash:
                            self._send_error(404, f"File not found: {file_path}")
                            return
                        obj = self.repo.store.retrieve(blob_hash)
                        if not obj:
                            self._send_error(404, f"Blob not found: {blob_hash}")
                            return
                        self._send_json({
                            "path": file_path,
                            "blob_hash": blob_hash,
                            "size": obj.size,
                            "content_base64": base64.b64encode(obj.data).decode("ascii"),
                        })

                    elif "/files" in rest:
                        # /states/<id>/files
                        state_id = rest.split("/files")[0]
                        state = self.repo.wsm.get_state(state_id)
                        if not state:
                            self._send_error(404, f"State not found: {state_id}")
                            return
                        files = self.repo.wsm._flatten_tree(state["root_tree"])
                        self._send_json({"state_id": state_id, "files": list(files.keys())})

                    else:
                        # /states/<id>
                        state_id = rest
                        state = self.repo.wsm.get_state(state_id)
                        if not state:
                            self._send_error(404, f"State not found: {state_id}")
                            return
                        self._send_json(state)

                elif path == "/diff":
                    a = params.get("a")
                    b = params.get("b")
                    if not a or not b:
                        self._send_error(400, "Missing 'a' and 'b' query parameters")
                        return
                    self._send_json(self.repo.diff(a, b))

                elif path == "/search":
                    q = params.get("q", "")
                    if not q:
                        self._send_error(400, "Missing 'q' query parameter")
                        return
                    self._send_json(self.repo.search(q))

                elif path.startswith("/objects/"):
                    obj_hash = path[len("/objects/"):]
                    obj = self.repo.store.retrieve(obj_hash)
                    if not obj:
                        self._send_error(404, f"Object not found: {obj_hash}")
                        return
                    self._send_json({
                        "hash": obj.hash,
                        "type": obj.type.value,
                        "size": obj.size,
                        "content_base64": base64.b64encode(obj.data).decode("ascii"),
                    })

                elif path == "/trace":
                    state = params.get("state")
                    self._send_json(self.repo.trace(state))

                elif path == "/workspaces":
                    ws_list = self.repo.workspaces()
                    self._send_json([w.to_dict() for w in ws_list])

                else:
                    self._send_error(404, f"Not found: {path}")

        except (ValueError, KeyError, FileNotFoundError) as e:
            self._send_error(400, str(e))
        except Exception:
            logger.exception("Unhandled error in GET %s", path)
            self._send_error(500, "Internal server error")

    def do_POST(self):
        path, params = self._parse_path()
        body = self._read_body()

        try:
            with self.repo_lock:
                if path == "/lanes":
                    name = body.get("name")
                    base = body.get("base")
                    if not name:
                        self._send_error(400, "Missing 'name'")
                        return
                    result = self.repo.create_lane(name, base)
                    self._send_json({"lane": result})

                elif path == "/workspaces":
                    name = body.get("name")
                    if not name:
                        self._send_error(400, "Missing 'name'")
                        return
                    ws = self.repo.workspace_create(
                        name,
                        lane=body.get("lane"),
                        state_id=body.get("state_id"),
                        agent_id=body.get("agent_id"),
                    )
                    self._send_json(ws.to_dict())

                elif path == "/snapshot":
                    workspace = body.get("workspace", "main")
                    state_id = self.repo.snapshot(workspace)
                    self._send_json({"state_id": state_id})

                elif path == "/propose":
                    agent = AgentIdentity(
                        agent_id=body.get("agent_id", "api"),
                        agent_type=body.get("agent_type", "api"),
                        model=body.get("model"),
                    )
                    tid = self.repo.propose(
                        from_state=body.get("from_state"),
                        to_state=body.get("to_state"),
                        prompt=body.get("prompt", ""),
                        agent=agent,
                        lane=body.get("lane"),
                        tags=body.get("tags"),
                    )
                    self._send_json({"transition_id": tid})

                elif path.startswith("/accept/"):
                    tid = path[len("/accept/"):]
                    status = self.repo.accept(
                        tid,
                        evaluator=body.get("evaluator", "api"),
                        summary=body.get("summary", ""),
                    )
                    self._send_json({"status": status.value})

                elif path.startswith("/reject/"):
                    tid = path[len("/reject/"):]
                    status = self.repo.reject(
                        tid,
                        evaluator=body.get("evaluator", "api"),
                        summary=body.get("summary", ""),
                    )
                    self._send_json({"status": status.value})

                elif path == "/commit":
                    agent = AgentIdentity(
                        agent_id=body.get("agent_id", "api"),
                        agent_type=body.get("agent_type", "api"),
                        model=body.get("model"),
                    )
                    result = self.repo.quick_commit(
                        workspace=body.get("workspace", "main"),
                        prompt=body.get("prompt", ""),
                        agent=agent,
                        lane=body.get("lane"),
                        tags=body.get("tags"),
                        auto_accept=body.get("auto_accept", False),
                        evaluator=body.get("evaluator", "auto"),
                    )
                    self._send_json(result)

                elif path == "/gc":
                    result = self.repo.gc(
                        dry_run=body.get("dry_run", True),
                        max_age_days=body.get("max_age_days", 30),
                    )
                    self._send_json({
                        "reachable_objects": result.reachable_objects,
                        "deleted_objects": result.deleted_objects,
                        "deleted_bytes": result.deleted_bytes,
                        "deleted_states": result.deleted_states,
                        "deleted_transitions": result.deleted_transitions,
                        "dry_run": result.dry_run,
                        "elapsed_ms": result.elapsed_ms,
                    })

                else:
                    self._send_error(404, f"Not found: {path}")

        except (ValueError, KeyError, FileNotFoundError) as e:
            self._send_error(400, str(e))
        except Exception:
            logger.exception("Unhandled error in POST %s", path)
            self._send_error(500, "Internal server error")

    def do_DELETE(self):
        path, params = self._parse_path()

        try:
            with self.repo_lock:
                if path.startswith("/workspaces/"):
                    name = path[len("/workspaces/"):]
                    self.repo.workspace_remove(name, force=True)
                    self._send_json({"deleted": name})
                else:
                    self._send_error(404, f"Not found: {path}")

        except (ValueError, KeyError, FileNotFoundError) as e:
            self._send_error(400, str(e))
        except Exception:
            logger.exception("Unhandled error in DELETE %s", path)
            self._send_error(500, "Internal server error")


class VexServer(ThreadingHTTPServer):
    """Thread-pooled HTTP server for Vex Repository.

    Uses ThreadingHTTPServer for concurrent request handling.
    A lock serializes access to the Repository to ensure SQLite thread safety.
    """

    repo: Repository | None
    _repo_path: str | None
    _repo_lock: threading.Lock

    def __init__(self, repo_or_path, host: str = "127.0.0.1", port: int = 7654):
        self._repo_lock = threading.Lock()
        if isinstance(repo_or_path, Repository):
            self.repo = repo_or_path
            self._repo_path = None
        else:
            self._repo_path = repo_or_path
            self.repo = None
        super().__init__((host, port), VexHandler)

    def _ensure_repo(self):
        """Open repo lazily in the serving thread to avoid SQLite threading issues."""
        with self._repo_lock:
            if self.repo is None and self._repo_path is not None:
                self.repo = Repository.find(Path(self._repo_path))

    def process_request(self, request, client_address):
        """Override to ensure repo is opened before handling requests."""
        self._ensure_repo()
        super().process_request(request, client_address)


def serve(repo_path, host="127.0.0.1", port=7654):
    """Start the Vex REST API server."""
    repo = Repository.find(Path(repo_path))
    server = VexServer(repo, host, port)
    actual_port = server.server_address[1]
    print(f"Vex server listening on {host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        repo.close()
