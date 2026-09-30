"""成果许可与收益的纯领域规则。

不访问数据库，便于直接单测：
- 日期区间 / 地域相交检测；
- 授权冲突（排他 vs 任何授权）判定；
- 收益按协议版本权属比例分成、争议份额冻结、尾差处理；
- 单笔收入分配依据的可重建解释。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import Any, Iterable, Mapping, Sequence


CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def periods_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """闭区间日期相交判断（到期日当天仍有效）。"""
    return parse_date(start_a) <= parse_date(end_b) and parse_date(start_b) <= parse_date(end_a)


def territories_intersect(codes_a: Iterable[str], codes_b: Iterable[str]) -> bool:
    return bool(set(codes_a) & set(codes_b))


@dataclass(frozen=True, slots=True)
class GrantShape:
    """冲突检测需要的授权最小形状。"""

    grant_id: str
    agreement_id: str
    licensee_id: str
    exclusive: bool
    start_date: str
    end_date: str
    territories: frozenset[str]
    state: str = "active"


def find_conflict(candidate: GrantShape, existing: Sequence[GrantShape]) -> GrantShape | None:
    """返回与候选授权冲突的已生效授权，没有则 None。

    规则（同一成果）：
    - 地域与有效期均相交时；
    - 候选或既有任一方为排他/独家，即冲突；
    - 双方都是普通非排他授权则可共存，同一承接方重复授权也阻止（避免双重签发）。
    """
    for other in existing:
        if other.state != "active" or other.grant_id == candidate.grant_id:
            continue
        if not periods_overlap(
            candidate.start_date, candidate.end_date, other.start_date, other.end_date
        ):
            continue
        if not territories_intersect(candidate.territories, other.territories):
            continue
        if candidate.exclusive or other.exclusive:
            return other
        if candidate.licensee_id == other.licensee_id:
            return other
    return None


@dataclass(frozen=True, slots=True)
class OwnerShare:
    owner_id: str
    owner_kind: str
    share: Decimal
    basis: str


@dataclass(frozen=True, slots=True)
class Freeze:
    owner_id: str
    amount: Decimal
    dispute_id: str


def distribute_revenue(
    gross: Decimal,
    owners: Sequence[OwnerShare],
    freezes: Mapping[str, Freeze] | None = None,
) -> dict[str, Any]:
    """按权属比例分成。

    - 分成金额按分四舍五入；尾差（分不尽）归份额最大的主体，保证分文不差；
    - freezes 中的主体对应金额进入冻结，不计入可结算；
    - 已结算部分永远不会经过这里重算（由 service 层保证）。
    """
    if gross < ZERO:
        raise ValueError("分成收入不能为负数")
    share_total = sum((item.share for item in owners), ZERO)
    if share_total != ONE:
        raise ValueError(f"权属份额合计必须等于 1，当前为 {share_total}")
    freezes = freezes or {}
    amounts: dict[str, Decimal] = {}
    for owner in owners:
        amounts[owner.owner_id] = quantize_money(gross * owner.share)
    remainder = quantize_money(gross - sum(amounts.values(), ZERO))
    if remainder != ZERO and owners:
        anchor = max(owners, key=lambda item: (item.share, item.owner_id)).owner_id
        amounts[anchor] += remainder
    lines: list[dict[str, Any]] = []
    settled = ZERO
    frozen_total = ZERO
    for owner in owners:
        amount = amounts[owner.owner_id]
        freeze = freezes.get(owner.owner_id)
        frozen_amount = ZERO
        if freeze is not None:
            # 冻结不超过该主体本次应得
            frozen_amount = min(amount, freeze.amount.quantize(CENT, rounding=ROUND_DOWN))
        frozen_amount = quantize_money(frozen_amount)
        payable = quantize_money(amount - frozen_amount)
        settled += payable
        frozen_total += frozen_amount
        lines.append(
            {
                "owner_id": owner.owner_id,
                "owner_kind": owner.owner_kind,
                "share": decimal_text(owner.share),
                "basis": owner.basis,
                "gross_amount": decimal_text(amount),
                "frozen_amount": decimal_text(frozen_amount),
                "payable_amount": decimal_text(payable),
                "dispute_id": None if freeze is None else freeze.dispute_id,
            }
        )
    return {
        "gross_amount": decimal_text(quantize_money(gross)),
        "settled_amount": decimal_text(settled),
        "frozen_amount": decimal_text(frozen_total),
        "lines": lines,
    }
