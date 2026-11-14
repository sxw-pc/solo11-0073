"""Read-only loopback HTTP review server."""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .models import InputError
from .views import error_page, render_index, render_run

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ARTIFACTS = {
    "report.json": "application/json; charset=utf-8",
    "report.md": "text/markdown; charset=utf-8",
    "report.html": "text/html; charset=utf-8",
    "run_manifest.json": "application/json; charset=utf-8",
    "ledger.sqlite": "application/vnd.sqlite3",
}


@dataclass
class RegisteredRun:
    report: dict[str, Any]
    directory: Path


def validate_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized not in _ALLOWED_HOSTS:
        raise InputError(
            "SERVER_INVALID_HOST",
            f"host must be one of {', '.join(sorted(_ALLOWED_HOSTS))}; got {host!r}",
        )
    return normalized


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        raise InputError("SERVER_INVALID_PORT", "port must be between 0 and 65535")
    return port


def load_runs(workdir: str | Path) -> dict[str, RegisteredRun]:
    root = Path(workdir)
    candidates: list[Path] = []
    direct = root / "report.json"
    if direct.is_file():
        candidates.append(direct)
    runs_dir = root / "runs"
    if runs_dir.is_dir():
        candidates.extend(sorted(runs_dir.glob("*/report.json")))
    found: dict[str, RegisteredRun] = {}
    for report_path in candidates:
        try:
            with report_path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        run_id = report.get("run_id")
        if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
            continue
        if run_id not in found:
            found[run_id] = RegisteredRun(report, report_path.parent)
    return found


def _sorted_reports(runs: dict[str, RegisteredRun]) -> list[dict[str, Any]]:
    return [
        item.report
        for item in sorted(
            runs.values(),
            key=lambda item: (
                str(item.report.get("generated_at", "")),
                str(item.report.get("run_id", "")),
            ),
            reverse=True,
        )
    ]


def _handler(workdir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SplitGuard/1.0"
        sys_version = ""

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                "form-action 'none'; base-uri 'none'",
            )
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status: int, text: str) -> None:
            self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

        def _json(self, status: int, value: Any) -> None:
            body = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _not_found(self) -> None:
            self._html(404, error_page(404, "请求的资源不存在"))

        def do_GET(self) -> None:
            path = unquote(urlsplit(self.path).path)
            if path == "/healthz":
                self._json(200, {"status": "ok"})
                return
            if path == "/":
                runs = load_runs(workdir)
                self._html(200, render_index(_sorted_reports(runs)))
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2 and parts[0] == "run":
                self._run_detail(parts[1])
                return
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "run":
                self._run_api(parts[2])
                return
            if len(parts) == 3 and parts[0] == "artifact":
                self._artifact(parts[1], parts[2])
                return
            self._not_found()

        def _run_detail(self, run_id: str) -> None:
            if not _RUN_ID.match(run_id):
                self._not_found()
                return
            registered = load_runs(workdir).get(run_id)
            if registered is None:
                self._not_found()
                return
            self._html(200, render_run(registered.report))

        def _run_api(self, run_id: str) -> None:
            if not _RUN_ID.match(run_id):
                self._json(404, {"error": "not_found"})
                return
            registered = load_runs(workdir).get(run_id)
            if registered is None:
                self._json(404, {"error": "not_found"})
                return
            self._json(200, registered.report)

        def _artifact(self, run_id: str, name: str) -> None:
            if not _RUN_ID.match(run_id) or name not in _ARTIFACTS:
                self._not_found()
                return
            registered = load_runs(workdir).get(run_id)
            if registered is None:
                self._not_found()
                return
            path = registered.directory / name
            if not path.is_file():
                self._not_found()
                return
            try:
                body = path.read_bytes()
            except OSError:
                self._html(500, error_page(500, "报告文件读取失败"))
                return
            self._send(
                200,
                body,
                _ARTIFACTS[name],
                {"Content-Disposition": f'attachment; filename="{name}"'},
            )

        def do_POST(self) -> None:
            self._send(
                405,
                error_page(405, "复核台为只读服务，不支持写入操作").encode("utf-8"),
                "text/html; charset=utf-8",
                {"Allow": "GET"},
            )

    return Handler


def create_server(
    workdir: str | Path, host: str, port: int
) -> ThreadingHTTPServer:
    normalized_host = validate_host(host)
    normalized_port = validate_port(port)
    handler = _handler(Path(workdir))

    if normalized_host == "::1":
        class GuardServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6
            daemon_threads = True
    else:
        class GuardServer(ThreadingHTTPServer):
            daemon_threads = True

    return GuardServer((normalized_host, normalized_port), handler)


def serve(workdir: str | Path, host: str, port: int) -> None:
    server = create_server(workdir, host, port)
    bound_host, bound_port = server.server_address[:2]
    display_host = f"[{bound_host}]" if ":" in str(bound_host) else str(bound_host)
    print(
        f"splitguard review server: http://{display_host}:{bound_port}/ "
        f"(read-only, workdir={Path(workdir)})",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
