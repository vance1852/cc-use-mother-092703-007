from __future__ import annotations

import unittest
from decimal import Decimal

from license_office.rules import (
    GrantShape,
    OwnerShare,
    distribute_revenue,
    find_conflict,
    periods_overlap,
    quantize_money,
    territories_intersect,
)


def shape(grant_id, exclusive, start, end, regions, licensee="l1", state="active"):
    return GrantShape(
        grant_id=grant_id,
        agreement_id="a1",
        licensee_id=licensee,
        exclusive=exclusive,
        start_date=start,
        end_date=end,
        territories=frozenset(regions),
        state=state,
    )


class RulesTests(unittest.TestCase):
    def test_period_overlap_closed_interval(self) -> None:
        self.assertTrue(periods_overlap("2026-01-01", "2026-06-30", "2026-06-30", "2026-12-31"))
        self.assertFalse(periods_overlap("2026-01-01", "2026-06-30", "2026-07-01", "2026-12-31"))

    def test_territory_intersection(self) -> None:
        self.assertTrue(territories_intersect(["CN-NORTH", "CN-EAST"], ["CN-SOUTH", "CN-EAST"]))
        self.assertFalse(territories_intersect(["CN-NORTH"], ["CN-SOUTH"]))

    def test_non_exclusive_different_licensees_coexist(self) -> None:
        existing = [shape("g1", False, "2026-01-01", "2026-12-31", ["CN-SOUTH"], licensee="l1")]
        candidate = shape("g2", False, "2026-01-01", "2026-12-31", ["CN-SOUTH"], licensee="l2")
        self.assertIsNone(find_conflict(candidate, existing))

    def test_exclusive_blocks_any_overlap(self) -> None:
        existing = [shape("g1", False, "2026-01-01", "2026-12-31", ["CN-NORTH"], licensee="l1")]
        candidate = shape("g2", True, "2026-06-01", "2027-01-31", ["CN-NORTH"], licensee="l2")
        conflict = find_conflict(candidate, existing)
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.grant_id, "g1")

    def test_existing_exclusive_blocks_new_plain_grant(self) -> None:
        existing = [shape("g1", True, "2026-01-01", "2026-12-31", ["CN-NORTH"], licensee="l1")]
        candidate = shape("g2", False, "2026-03-01", "2026-09-30", ["CN-NORTH"], licensee="l2")
        self.assertEqual(find_conflict(candidate, existing).grant_id, "g1")

    def test_same_licensee_duplicate_blocked_even_when_non_exclusive(self) -> None:
        existing = [shape("g1", False, "2026-01-01", "2026-12-31", ["CN-SOUTH"], licensee="l1")]
        candidate = shape("g2", False, "2026-06-01", "2026-08-31", ["CN-SOUTH"], licensee="l1")
        self.assertEqual(find_conflict(candidate, existing).grant_id, "g1")

    def test_adjacent_window_and_other_region_do_not_conflict(self) -> None:
        existing = [shape("g1", True, "2026-01-01", "2026-06-30", ["CN-NORTH"], licensee="l1")]
        adjacent = shape("g2", False, "2026-07-01", "2026-12-31", ["CN-NORTH"], licensee="l2")
        other_region = shape("g3", True, "2026-01-01", "2026-12-31", ["CN-EAST"], licensee="l2")
        self.assertIsNone(find_conflict(adjacent, existing))
        self.assertIsNone(find_conflict(other_region, existing))

    def test_inactive_grants_are_ignored(self) -> None:
        existing = [shape("g1", True, "2026-01-01", "2026-12-31", ["CN-NORTH"], licensee="l1", state="terminated")]
        candidate = shape("g2", True, "2026-01-01", "2026-12-31", ["CN-NORTH"], licensee="l2")
        self.assertIsNone(find_conflict(candidate, existing))

    def test_distribution_sums_exactly_with_remainder_to_largest(self) -> None:
        owners = [
            OwnerShare("u", "university_team", Decimal("0.60"), "办法"),
            OwnerShare("e", "enterprise", Decimal("0.40"), "协议"),
        ]
        result = distribute_revenue(Decimal("100.01"), owners)
        totals = sum((Decimal(line["gross_amount"]) for line in result["lines"]), Decimal("0"))
        self.assertEqual(totals, Decimal("100.01"))
        by_owner = {line["owner_id"]: line["gross_amount"] for line in result["lines"]}
        self.assertEqual(by_owner, {"u": "60.01", "e": "40.00"})

    def test_distribution_rejects_bad_share_total(self) -> None:
        owners = [OwnerShare("u", "university_team", Decimal("0.9"), "")]
        with self.assertRaises(ValueError):
            distribute_revenue(Decimal("100"), owners)

    def test_freeze_only_covers_named_owner(self) -> None:
        owners = [
            OwnerShare("u", "university_team", Decimal("0.60"), "办法"),
            OwnerShare("e", "enterprise", Decimal("0.40"), "协议"),
        ]
        from license_office.rules import Freeze

        result = distribute_revenue(
            Decimal("100000"),
            owners,
            {"e": Freeze(owner_id="e", amount=Decimal("40000"), dispute_id="d1")},
        )
        lines = {line["owner_id"]: line for line in result["lines"]}
        self.assertEqual(lines["u"]["frozen_amount"], "0.00")
        self.assertEqual(lines["u"]["payable_amount"], "60000.00")
        self.assertEqual(lines["e"]["frozen_amount"], "40000.00")
        self.assertEqual(lines["e"]["payable_amount"], "0.00")
        self.assertEqual(result["settled_amount"], "60000.00")
        self.assertEqual(result["frozen_amount"], "40000.00")

    def test_quantize_money_half_up(self) -> None:
        self.assertEqual(quantize_money(Decimal("1.005")), Decimal("1.01"))


if __name__ == "__main__":
    unittest.main()
