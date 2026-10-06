"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .lineage import LineageService
from .storage import Database


def _jsonable(value: Any) -> Any:
    """把数据类、元组等结构转换为可 JSON 序列化的值。"""

    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def route(service: LineageService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = dict(body or {})
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    segments = [segment for segment in parsed.path.split("/") if segment]
    actor_id = headers.get("X-Actor-Id", "")

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        # ---------------------------------------------------------- 基础能力
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            items = service.list_domain_data(site_id, q("category"))
            return 200, {"items": [_jsonable(item) for item in items]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(q("after_sequence", "0"))
            return 200, {"items": service.audit_events(after)}

        # ---------------------------------------------------------- 证据
        if method == "POST" and parsed.path == "/evidence":
            receipt = service.import_evidence(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and segments == ["evidence", "sweep-expired"]:
            return 200, service.sweep_expired(actor_id=actor_id)
        if method == "POST" and len(segments) == 3 and segments[0] == "evidence":
            evidence_id, action = segments[1], segments[2]
            if action == "retract":
                receipt = service.retract_evidence(actor_id=actor_id, evidence_id=evidence_id, **body)
                return 200 if receipt.replayed else 201, receipt.__dict__
            if action == "expire":
                receipt = service.expire_evidence(actor_id=actor_id, evidence_id=evidence_id, **body)
                return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and segments == ["evidence"]:
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            items = service.list_evidence(
                actor_id=actor_id, site_id=site_id, evidence_key=q("evidence_key"),
                evidence_type=q("evidence_type"), status=q("status"),
            )
            return 200, {"items": [_jsonable(item) for item in items]}
        if method == "GET" and len(segments) == 2 and segments[0] == "evidence":
            return 200, _jsonable(service.get_evidence(actor_id=actor_id, evidence_id=segments[1]))
        if method == "GET" and len(segments) == 3 and segments[0] == "evidence" and segments[2] == "lineage":
            return 200, _jsonable(service.evidence_lineage(actor_id=actor_id, evidence_id=segments[1]))

        # ---------------------------------------------------------- 运行
        if method == "POST" and parsed.path == "/runs":
            receipt = service.register_run(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "runs" and segments[2] == "result":
            body["run_id"] = segments[1]
            receipt = service.attach_run_result(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and segments == ["runs"]:
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            items = service.list_runs(actor_id=actor_id, site_id=site_id)
            return 200, {"items": [_jsonable(item) for item in items]}
        if method == "GET" and len(segments) == 2 and segments[0] == "runs":
            return 200, _jsonable(service.get_run(actor_id=actor_id, run_id=segments[1]))

        # ---------------------------------------------------------- 结论
        if method == "POST" and parsed.path == "/conclusions":
            receipt = service.create_conclusion(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/conclusions/revise":
            receipt = service.revise_conclusion(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "conclusions" and segments[2] == "publish":
            body.setdefault("request_id", f"publish:{segments[1]}")
            receipt = service.publish_conclusion(actor_id=actor_id, conclusion_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and segments == ["conclusions"]:
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            items = service.list_conclusions(
                actor_id=actor_id, site_id=site_id, conclusion_key=q("conclusion_key"))
            return 200, {"items": [_jsonable(item) for item in items]}
        if method == "GET" and len(segments) == 2 and segments[0] == "conclusions":
            return 200, _jsonable(service.get_conclusion(actor_id=actor_id, conclusion_id=segments[1]))
        if method == "GET" and len(segments) == 3 and segments[0] == "conclusions" and segments[2] == "affected-runs":
            return 200, _jsonable(service.affected_runs(actor_id=actor_id, conclusion_id=segments[1]))

        # ---------------------------------------------------------- 影响说明
        if method == "GET" and segments == ["impact-statements"]:
            items = service.list_impact_statements(
                actor_id=actor_id, conclusion_id=q("conclusion_id"),
                evidence_id=q("evidence_id"), run_id=q("run_id"))
            return 200, {"items": [_jsonable(item) for item in items]}

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: LineageService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动安全评估证据谱系服务")
    parser.add_argument("--database", default="lineage.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = LineageService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
