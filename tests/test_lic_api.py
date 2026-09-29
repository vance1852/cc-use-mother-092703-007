from __future__ import annotations

import json
import sqlite3
import unittest

from license_revenue.api import JsonApplication
from license_revenue.service import LicensingService


def json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class LicApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(LicensingService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _call(self, method: str, path: str, payload: dict | None = None, actor: str = "tm"):
        headers = {"X-Actor-Id": actor}
        body = json_body(payload) if payload is not None else b""
        return self.app.handle(method, path, headers, body)

    def _bootstrap(self) -> None:
        for uid, role in (("tm", "manager"), ("legal", "legal"), ("fin", "finance"), ("audit", "auditor")):
            self._call("POST", "/users", {"user_id": uid, "display_name": uid, "role": role})
        self._call("POST", "/achievements", {
            "achievement_id": "mat-1", "title": "新材料工艺",
            "field_kind": "patent", "completed_on": "2025-11-01",
        })
        self._call("POST", "/agreements", {
            "agreement_id": "agr-1", "achievement_id": "mat-1", "version_label": "v1",
            "effective_on": "2026-01-01", "expires_on": "2030-12-31",
            "ownership": [
                {"owner_id": "univ", "share_percent": "60"},
                {"owner_id": "equipco", "share_percent": "40"},
            ],
        })
        self._call("POST", "/agreements/agr-1/versions/1/ratify", {}, actor="legal")

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor(self) -> None:
        response = self.app.handle("POST", "/achievements", body=json_body({}))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_full_http_workflow_and_conflict_details(self) -> None:
        self._bootstrap()
        issued = self._call("POST", "/licenses", {
            "license_id": "lic-a", "agreement_id": "agr-1", "version_no": 1,
            "licensee_id": "co-a", "license_type": "exclusive", "regions": ["CN:BJ"],
            "valid_from": "2026-02-01", "valid_until": "2027-12-31",
            "improvement_policy": "joint", "idempotency_key": "k1",
        }, actor="legal")
        self.assertEqual(issued.status, 201)

        conflict = self._call("POST", "/licenses", {
            "license_id": "lic-b", "agreement_id": "agr-1", "version_no": 1,
            "licensee_id": "co-b", "license_type": "non_exclusive", "regions": ["CN:BJ:HD"],
            "valid_from": "2026-02-01", "valid_until": "2027-12-31",
            "improvement_policy": "joint", "idempotency_key": "k2",
        }, actor="legal")
        self.assertEqual(conflict.status, 409)
        self.assertEqual(conflict.body["error"]["code"], "conflict")
        self.assertTrue(conflict.body["error"]["details"])

        estimate = self._call("POST", "/revenue/estimate", {
            "installment_id": "pay-1", "license_id": "lic-a", "period_label": "2026Q1",
            "gross_amount": "1000.00", "idempotency_key": "rk1",
        }, actor="fin")
        self.assertEqual(estimate.status, 201)
        assess = self._call("POST", "/revenue/installments/pay-1/assess", {"gross_amount": "1000.00"}, actor="fin")
        self.assertEqual(assess.status, 200)
        explain = self._call("GET", "/revenue/installments/pay-1/explain", actor="audit")
        self.assertEqual(explain.status, 200)
        self.assertEqual(len(explain.body["allocation_basis"]), 2)

        chain = self.app.handle(
            "GET", "/chains?achievement_id=mat-1", {"x-actor-id": "tm"}
        )
        self.assertEqual(chain.status, 200)
        self.assertEqual(chain.body["licenses"][0]["license_id"], "lic-a")

        audit = self._call("GET", "/audit/chain", actor="audit")
        self.assertTrue(audit.body["valid"])

    def test_renewal_extension_blocked_over_http(self) -> None:
        self._bootstrap()
        self._call("POST", "/licenses", {
            "license_id": "lic-a", "agreement_id": "agr-1", "version_no": 1,
            "licensee_id": "co-a", "license_type": "exclusive", "regions": ["CN:BJ"],
            "valid_from": "2026-02-01", "valid_until": "2027-12-31",
            "improvement_policy": "joint", "idempotency_key": "k1",
        }, actor="legal")
        opened = self._call("POST", "/licenses/lic-a/renewals", {"note": "谈判中"}, actor="legal")
        self.assertEqual(opened.status, 201)
        blocked = self._call("POST", "/licenses/lic-a/extend", {"new_valid_until": "2028-06-30"}, actor="legal")
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.body["error"]["code"], "invalid_state")


if __name__ == "__main__":
    unittest.main()
