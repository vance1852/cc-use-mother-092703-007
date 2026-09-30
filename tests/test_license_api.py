from __future__ import annotations

import json
import sqlite3
import unittest

from license_office.api import JsonApplication
from license_office.service import LicenseOfficeService


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(LicenseOfficeService(self.connection))
        self.actors = {"X-Actor-Id": "mgr"}

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "mgr"):
        return self.app.handle(
            "POST",
            path,
            {"X-Actor-Id": actor, "Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )

    def _setup(self) -> None:
        self._post("/users", {"user_id": "mgr", "display_name": "管理", "role": "manager"})
        self.assertEqual(
            self._post("/achievements", {"achievement_id": "ach-1", "name": "新工艺", "domain": "材料"}).status,
            201,
        )
        self.assertEqual(
            self._post("/licensees", {"licensee_id": "l1", "name": "企业一", "credit_code": "C1", "contact": "x"}).status,
            201,
        )
        agreement = {
            "agreement_id": "agr-1",
            "achievement_id": "ach-1",
            "title": "协议",
            "version": "v1",
            "effective_date": "2025-01-01",
            "expiry_date": "2030-12-31",
            "ownership": [
                {"owner_id": "uni", "owner_kind": "university_team", "share": "0.6", "basis": "办法"},
                {"owner_id": "co", "owner_kind": "enterprise", "share": "0.4", "basis": "协议"},
            ],
        }
        self.assertEqual(self._post("/agreements", agreement, actor="mgr").status, 201)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_full_flow_over_http(self) -> None:
        self._post("/users", {"user_id": "mgr", "display_name": "管理", "role": "manager"})
        self._post("/users", {"user_id": "legal", "display_name": "法务", "role": "legal"})
        self._post("/users", {"user_id": "fin", "display_name": "财务", "role": "finance"})
        self._post("/users", {"user_id": "aud", "display_name": "审计", "role": "auditor"})
        self._post("/achievements", {"achievement_id": "ach-1", "name": "新工艺", "domain": "材料"})
        self._post("/licensees", {"licensee_id": "l1", "name": "企业一", "credit_code": "C1", "contact": "x"})
        agreement = {
            "agreement_id": "agr-1", "achievement_id": "ach-1", "title": "协议", "version": "v1",
            "effective_date": "2025-01-01", "expiry_date": "2030-12-31",
            "ownership": [
                {"owner_id": "uni", "owner_kind": "university_team", "share": "0.6", "basis": "办法"},
                {"owner_id": "co", "owner_kind": "enterprise", "share": "0.4", "basis": "协议"},
            ],
        }
        self.assertEqual(self._post("/agreements", agreement, actor="legal").status, 201)
        grant_payload = {
            "grant_id": "g1", "agreement_id": "agr-1", "licensee_id": "l1", "grant_kind": "non_exclusive",
            "territories": [{"region_code": "CN-NORTH", "region_name": "华北"}],
            "fields_of_use": "商业实施", "start_date": "2025-02-01", "end_date": "2027-12-31",
            "royalty": {"royalty_type": "sales_rate", "rate": "0.05", "fixed_amount": None, "note": ""},
        }
        self.assertEqual(self._post("/grants", grant_payload).status, 201)
        # 冲突签发通过 HTTP 返回 409
        conflict = self._post("/grants", dict(grant_payload, grant_id="g2", grant_kind="exclusive"))
        self.assertEqual(conflict.status, 409)
        estimate = self._post("/revenue/periods", {
            "period_id": "p1", "grant_id": "g1", "period_date": "2026-03-31",
            "estimated_amount": "100000", "idempotency_key": "k1",
        }, actor="fin")
        self.assertEqual(estimate.status, 201)
        assess = self._post("/revenue/periods/assess", {"period_id": "p1", "assessed_amount": "102000"}, actor="fin")
        self.assertEqual(assess.status, 200)
        settle = self._post("/revenue/periods/settle", {"period_id": "p1"}, actor="fin")
        self.assertEqual(settle.body["status"], "settled")
        explain = self.app.handle("GET", "/revenue/periods/p1/explain", {"X-Actor-Id": "aud"})
        self.assertEqual(explain.status, 200)
        self.assertEqual(explain.body["agreement"]["version"], "v1")
        chain = self.app.handle("GET", "/chains?achievement_id=ach-1", {"X-Actor-Id": "aud"})
        self.assertEqual(chain.status, 200)
        self.assertEqual(len(chain.body["grants"]), 1)
        audit = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "aud"})
        self.assertTrue(audit.body["valid"])

    def test_json_error_shape_and_missing_actor(self) -> None:
        bad = self.app.handle("POST", "/achievements", {}, b"not-json")
        self.assertEqual(bad.status, 422)
        self.assertEqual(bad.body["error"]["code"], "validation_failed")
        missing = self.app.handle("GET", "/chains")
        self.assertEqual(missing.status, 422)

    def test_agreement_get(self) -> None:
        self._setup()
        response = self.app.handle("GET", "/agreements/agr-1", {"X-Actor-Id": "mgr"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["version"], "v1")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "mgr"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
