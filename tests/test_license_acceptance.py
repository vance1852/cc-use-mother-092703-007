from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from license_office.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class LicenseAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        # 三类协议版本都已登记，旧版本被顺序替代
        self.assertEqual(result["agreement_versions"], ["v1-2025", "v2-2026", "v3-2027"])
        # 所有被规则阻止的动作
        blocked = "|".join(result["blocked_actions"])
        self.assertIn("exclusive-overlap", blocked)
        self.assertIn("same-licensee-dup", blocked)
        self.assertIn("issue-under-superseded", blocked)
        self.assertIn("extend-during-renewal", blocked)
        self.assertIn("issue-during-renewal", blocked)
        self.assertIn("reassess-settled", blocked)
        # 历史收益仍按 v1 的 60/40 解释
        self.assertEqual(
            set(result["north_history"]["ownership"]),
            {("uni-lab", "0.60"), ("equip-co", "0.40")},
        )
        self.assertEqual(result["north_history"]["settled_amount"], "102000.00")
        # 争议只冻结相关份额，先结 60%，处理后补齐
        self.assertEqual(result["south_dispute"]["frozen_at_first_settlement"], "32000.00")
        self.assertEqual(result["south_dispute"]["settled_before_resolution"], "48000.00")
        self.assertEqual(result["south_dispute"]["settled_after_resolution"], "80000.00")
        # v3 协议下新收益按 50/35/15 分成
        self.assertEqual(
            result["east_v3_split"],
            {"uni-lab": "50000.00", "equip-co": "35000.00", "tto-platform": "15000.00"},
        )
        # 许可链：旧授权终止、续约指向新授权
        chain = result["chains"]["by_achievement"]
        self.assertIn("g-north", chain["grants"])
        self.assertIn("g-north-2", chain["grants"])
        self.assertEqual(result["chains"]["by_licensee_north"], ["g-north", "g-north-2"])
        self.assertIn(("g-north", "signed", "g-north-2"), chain["renewals"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
