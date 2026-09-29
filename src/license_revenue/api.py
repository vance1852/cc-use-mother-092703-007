"""无第三方依赖的成果许可与收益 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import LicensingError, ValidationFailed
from .service import LicensingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: LicensingService) -> None:
        self.service = service
        # 离线单机服务：所有请求共享一个 SQLite 连接，用锁串行化事务。
        self.lock = threading.Lock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)

            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                ))

            if method == "POST" and path == "/achievements":
                return Response(201, self.service.register_achievement(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "achievements":
                return Response(200, self._achievement(parts[1]))

            if method == "POST" and path == "/agreements":
                return Response(201, self.service.draft_agreement(actor, payload))
            if method == "POST" and len(parts) == 5 and parts[0] == "agreements" and parts[2] == "versions" and parts[4] == "ratify":
                return Response(200, self.service.ratify_agreement(actor, parts[1], int(parts[3])))
            if method == "GET" and len(parts) == 4 and parts[0] == "agreements" and parts[2] == "versions":
                return Response(200, self.service.agreement_snapshot(parts[1], int(parts[3])))

            if method == "POST" and path == "/licenses":
                return Response(201, self.service.issue_license(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "licenses":
                return Response(200, self.service.license_snapshot(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "licenses" and parts[2] == "terminate":
                return Response(200, self.service.terminate_license(actor, parts[1], payload.get("reason", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "licenses" and parts[2] == "extend":
                return Response(200, self.service.extend_license(actor, parts[1], payload["new_valid_until"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "licenses" and parts[2] == "renewals":
                return Response(201, self.service.open_renewal(actor, parts[1], payload.get("note", "")))

            if method == "POST" and len(parts) == 3 and parts[0] == "renewals" and parts[2] == "complete":
                return Response(201, self.service.complete_renewal(actor, int(parts[1]), payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "renewals" and parts[2] == "abandon":
                return Response(200, self.service.abandon_renewal(actor, int(parts[1]), payload.get("note", "")))

            if method == "POST" and path == "/improvements":
                return Response(201, self.service.file_improvement(actor, payload))

            if method == "POST" and path == "/revenue/estimate":
                return Response(201, self.service.estimate_revenue(actor, payload))
            if method == "GET" and len(parts) == 3 and parts[0] == "revenue" and parts[1] == "installments":
                return Response(200, self.service.installment_snapshot(parts[2]))
            if method == "GET" and len(parts) == 4 and parts[0] == "revenue" and parts[1] == "installments" and parts[3] == "explain":
                return Response(200, self.service.explain_installment(actor, parts[2]))
            if method == "POST" and len(parts) == 4 and parts[0] == "revenue" and parts[1] == "installments" and parts[3] == "assess":
                return Response(200, self.service.assess_revenue(actor, parts[2], payload["gross_amount"], payload.get("note", "")))
            if method == "POST" and len(parts) == 4 and parts[0] == "revenue" and parts[1] == "installments" and parts[3] == "settle":
                payees = payload.get("payee_ids")
                return Response(200, self.service.settle_revenue(actor, parts[2], payees))
            if method == "POST" and len(parts) == 4 and parts[0] == "revenue" and parts[1] == "installments" and parts[3] == "disputes":
                return Response(201, self.service.open_dispute(actor, parts[2], payload["payee_id"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, self.service.resolve_dispute(
                    actor, int(parts[1]), payload["resolution"],
                    adjusted_amount=payload.get("adjusted_amount"),
                ))

            if method == "GET" and path == "/chains":
                return Response(200, self.service.rebuild_license_chain(
                    actor,
                    achievement_id=query.get("achievement_id", [None])[0],
                    licensee_id=query.get("licensee_id", [None])[0],
                ))

            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LicensingError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            details = getattr(exc, "details", None)
            if details is not None:
                error["details"] = details
            return Response(exc.status, {"error": error})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})

    def _achievement(self, achievement_id: str) -> dict[str, Any]:
        row = self.service.connection.execute(
            "SELECT achievement_id,title,field_kind,completed_on,basis_note,created_at "
            "FROM achievements WHERE achievement_id=?",
            (achievement_id,),
        ).fetchone()
        if row is None:
            from .errors import NotFound

            raise NotFound("成果不存在")
        return dict(row)


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LicenseRevenue/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with application.lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动成果许可与收益管理服务")
    parser.add_argument("--database", type=Path, default=Path("license_revenue.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(LicensingService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
