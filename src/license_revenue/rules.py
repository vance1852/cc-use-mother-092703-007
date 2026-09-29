"""成果许可与收益的纯领域规则。

本模块不接触数据库，所有函数均为确定性计算，便于单测和离线复算：

- 地域范围采用冒号层级编码（``CN:BJ`` 从属于 ``CN``），``WORLD`` 覆盖一切；
- 独占/排他/普通许可在时间区间、地域、领域三维度上的冲突判定；
- 分成金额按 0.01 元舍入，余数确定性地归入最大份额，保证分配合计等于收入总额。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Sequence


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


ZERO = Decimal("0")
HUNDRED = Decimal("100")
MONEY_QUANTUM = Decimal("0.01")
SHARE_QUANTUM = Decimal("0.0001")
WORLD = "WORLD"
ALL_SCOPE = "all"


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def normalize_share(value: Decimal) -> Decimal:
    return value.quantize(SHARE_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def region_overlap(code_a: str, code_b: str) -> bool:
    """两个地域编码是否相交（相同或存在祖属关系）。"""
    a = code_a.upper()
    b = code_b.upper()
    if a == WORLD or b == WORLD:
        return True
    if a == b:
        return True
    return a.startswith(b + ":") or b.startswith(a + ":")


def scope_overlap(scope_a: str, scope_b: str) -> bool:
    """领域范围相交：all 覆盖一切，否则必须同名。"""
    a = scope_a.strip()
    b = scope_b.strip()
    return a == ALL_SCOPE or b == ALL_SCOPE or a == b


@dataclass(frozen=True, slots=True)
class LicenseGrant:
    license_id: str
    licensee_id: str
    license_type: str
    region_code: str
    field_scope: str
    valid_from: str
    valid_until: str | None


def effective_grants(
    license_id: str,
    licensee_id: str,
    license_type: str,
    regions: Sequence[Mapping[str, str]],
    global_scope: str,
    valid_from: str,
    valid_until: str | None,
) -> list[LicenseGrant]:
    """把许可证的全局领域限定与逐地域领域限定求交，展开为授权网格。"""
    grants: list[LicenseGrant] = []
    for region in regions:
        region_scope = region.get("field_scope", ALL_SCOPE)
        if global_scope == ALL_SCOPE:
            scope = region_scope
        elif region_scope == ALL_SCOPE:
            scope = global_scope
        elif region_scope == global_scope:
            scope = global_scope
        else:
            # 全局限定与该地域限定互不相交，此地域不构成授权。
            continue
        grants.append(
            LicenseGrant(
                license_id=license_id,
                licensee_id=licensee_id,
                license_type=license_type,
                region_code=region["region_code"],
                field_scope=scope,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        )
    return grants


def date_overlap(
    start_a: str,
    end_a: str | None,
    start_b: str,
    end_b: str | None,
) -> bool:
    """闭区间/半开区间相交判定，None 表示开放结束。"""
    if end_a is not None and end_a < start_b:
        return False
    if end_b is not None and end_b < start_a:
        return False
    return True


def grants_intersect(grant_a: LicenseGrant, grant_b: LicenseGrant) -> bool:
    return (
        region_overlap(grant_a.region_code, grant_b.region_code)
        and scope_overlap(grant_a.field_scope, grant_b.field_scope)
        and date_overlap(grant_a.valid_from, grant_a.valid_until, grant_b.valid_from, grant_b.valid_until)
    )


_EXCLUSIVE = "exclusive"
_SOLE = "sole"


def type_conflicts(type_a: str, type_b: str, same_licensee: bool) -> bool:
    """许可类型是否互斥。

    - 独占（exclusive）与任何其他许可互斥；
    - 排他（sole，仅保留许可方自身）与任何其他承接方许可互斥；
    - 向同一承接方重复签发重叠许可同样视为冲突，续约必须走显式更替。
    """
    if same_licensee:
        return True
    if type_a == _EXCLUSIVE or type_b == _EXCLUSIVE:
        return True
    if type_a == _SOLE or type_b == _SOLE:
        return True
    return False


@dataclass(frozen=True, slots=True)
class GrantConflict:
    existing_license_id: str
    existing_licensee_id: str
    existing_type: str
    region_code: str
    field_scope: str
    reason: str


def find_grant_conflicts(
    candidate: Sequence[LicenseGrant],
    existing: Sequence[LicenseGrant],
) -> list[GrantConflict]:
    """返回候选授权网格与所有现存授权的冲突明细（去重）。"""
    conflicts: list[GrantConflict] = []
    seen: set[tuple[str, str, str]] = set()
    for new_grant in candidate:
        for old_grant in existing:
            if not grants_intersect(new_grant, old_grant):
                continue
            same_licensee = new_grant.licensee_id == old_grant.licensee_id
            if not type_conflicts(new_grant.license_type, old_grant.license_type, same_licensee):
                continue
            if same_licensee:
                reason = "同一承接方在重叠时间和地域内已持有许可，续约必须显式更替旧许可"
            elif _EXCLUSIVE in (new_grant.license_type, old_grant.license_type):
                reason = "独占许可与重叠授权范围冲突"
            else:
                reason = "排他许可不能与重叠时间、地域、领域的其他许可并存"
            key = (old_grant.license_id, new_grant.region_code, new_grant.field_scope)
            if key in seen:
                continue
            seen.add(key)
            conflicts.append(
                GrantConflict(
                    existing_license_id=old_grant.license_id,
                    existing_licensee_id=old_grant.licensee_id,
                    existing_type=old_grant.license_type,
                    region_code=new_grant.region_code,
                    field_scope=new_grant.field_scope,
                    reason=reason,
                )
            )
    return conflicts


def allocate_pool(pool: Decimal, weights: Sequence[tuple[str, Decimal, str]]) -> list[dict[str, object]]:
    """把一个资金池按相对权重（不必合计为 100）分完。

    用于部分行已结算后重新核定：已结算行锁定，剩余金额仅在未结算行之间
    按其原分成比例的相对值分配，余数确定性归入最大行。
    """
    pool_q = quantize_money(pool)
    if pool_q < ZERO:
        raise ValueError("分配资金池不能为负数")
    positive = [(payee, weight, source) for payee, weight, source in weights if weight > ZERO]
    if not positive:
        raise ValueError("至少需要一个正权重")
    total_weight = sum((item[1] for item in positive), ZERO)
    allocated: list[dict[str, object]] = []
    subtotal = ZERO
    for payee, weight, source in positive:
        amount = quantize_money(pool_q * weight / total_weight)
        subtotal += amount
        allocated.append(
            {
                "payee_id": payee,
                "weight_percent": normalize_share(weight),
                "allocated_amount": amount,
                "source": source,
            }
        )
    remainder = pool_q - subtotal
    if remainder != ZERO:
        anchor = min(
            range(len(allocated)),
            key=lambda index: (-allocated[index]["allocated_amount"], allocated[index]["payee_id"]),
        )
        allocated[anchor]["allocated_amount"] = quantize_money(
            allocated[anchor]["allocated_amount"] + remainder
        )
        allocated[anchor]["remainder_adjusted"] = True
    return allocated


@dataclass(frozen=True, slots=True)
class SplitLine:
    payee_id: str
    share_percent: Decimal
    source: str = "explicit"


def allocate_revenue(gross: Decimal, split: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """按分成比例分配收入，余数（0.01 元粒度）归入最大份额方。

    冻结金额不在这里处理——调用方按收款方逐行判定冻结状态，
    分配行的合计始终严格等于毛收入，避免争议金额改变非争议方所得。
    """
    lines = [
        SplitLine(
            payee_id=str(item["payee_id"]),
            share_percent=Decimal(str(item["share_percent"])),
            source=str(item.get("source", "explicit")),
        )
        for item in split
    ]
    if not lines:
        raise ValueError("分成方案不能为空")
    total_share = sum((line.share_percent for line in lines), ZERO)
    if total_share <= ZERO or total_share > HUNDRED:
        raise ValueError("分成比例之和必须在 0 到 100% 之间")
    gross_q = quantize_money(gross)
    if gross_q < ZERO:
        raise ValueError("收入金额不能为负数")
    allocated: list[dict[str, object]] = []
    subtotal = ZERO
    for line in lines:
        amount = quantize_money(gross_q * line.share_percent / HUNDRED)
        subtotal += amount
        allocated.append(
            {
                "payee_id": line.payee_id,
                "share_percent": line.share_percent,
                "allocated_amount": amount,
                "source": line.source,
            }
        )
    remainder = gross_q - subtotal
    if remainder != ZERO and total_share == HUNDRED:
        # 分成合计为 100% 时，舍入余数（分级粒度）确定性归入最大行；
        # 合计不足 100% 的差额是机构留存，不得分配给收款方。
        anchor = min(
            range(len(allocated)),
            key=lambda index: (-allocated[index]["allocated_amount"], allocated[index]["payee_id"]),
        )
        allocated[anchor]["allocated_amount"] = quantize_money(
            allocated[anchor]["allocated_amount"] + remainder
        )
        allocated[anchor]["remainder_adjusted"] = True
    return allocated
