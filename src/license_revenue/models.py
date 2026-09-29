"""成果许可领域输入契约。

所有对象在进入服务前先在此完成结构与取值校验，保证：

- 权属比例之和必须等于 100%；
- 同一协议版本内地域编码不得重复；
- 收益分成比例之和不得超过权属比例口径（允许小于，差额作为机构留存）；
- 许可的生效/失效日期构成合法区间。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed
from .rules import HUNDRED, ZERO, normalize_share, quantize_money

IDENTIFIER = re.compile(r"^[A-Za-z0-9一-鿿][A-Za-z0-9_.::\-一-鿿（）()]{0,63}$")
FIELD_KINDS = {"patent", "know_how", "software", "data", "material"}
LICENSE_TYPES = {"exclusive", "sole", "non_exclusive"}
IMPROVEMENT_OWNERSHIP = {"licensor", "licensee", "joint", "case_by_case"}
REVENUE_STATES = {"estimated", "assessed", "settled", "disputed"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def share_value(value: object, field: str) -> Decimal:
    """解析百分比形式的比例（如 60 或 '60%' 均表示 60%），返回百分数。"""
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            value = text[:-1]
    return normalize_share(decimal_value(value, field, minimum=ZERO, maximum=HUNDRED))


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def date_range(start: object, end: object, start_field: str, end_field: str) -> tuple[str, str | None]:
    start_text = date_text(start, start_field)
    end_text = None if end is None else date_text(end, end_field)
    if end_text is not None and end_text < start_text:
        raise ValidationFailed(f"{end_field} 不能早于 {start_field}")
    return start_text, end_text


def _ownership_entries(raw: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValidationFailed(f"{field} 必须是非空数组")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"{field}[{index}] 必须是对象")
        owner = identifier(item.get("owner_id"), f"{field}[{index}].owner_id")
        if owner in seen:
            raise ValidationFailed(f"{field} 中权属方 {owner} 重复")
        seen.add(owner)
        share = share_value(item.get("share_percent"), f"{field}[{index}].share_percent")
        kind = required_text(item.get("kind", "patent"), f"{field}[{index}].kind", 16)
        if kind not in FIELD_KINDS:
            raise ValidationFailed(f"{field}[{index}].kind 不是受支持的成果形态")
        note = item.get("note", "")
        if note is not None and not isinstance(note, str):
            raise ValidationFailed(f"{field}[{index}].note 必须是文本")
        entries.append({"owner_id": owner, "share_percent": share, "kind": kind, "note": note or ""})
    total = sum((item["share_percent"] for item in entries), ZERO)
    if total != HUNDRED:
        raise ValidationFailed(f"{field} 比例之和必须等于 100.00%，当前为 {quantize_money(total)}%")
    return entries


@dataclass(frozen=True, slots=True)
class AchievementRegistration:
    achievement_id: str
    title: str
    field_kind: str
    completed_on: str
    basis_note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AchievementRegistration":
        kind = required_text(raw.get("field_kind", "patent"), "field_kind", 16)
        if kind not in FIELD_KINDS:
            raise ValidationFailed("field_kind 不是受支持的成果形态")
        return cls(
            achievement_id=identifier(raw.get("achievement_id"), "achievement_id"),
            title=required_text(raw.get("title"), "title"),
            field_kind=kind,
            completed_on=date_text(raw.get("completed_on"), "completed_on"),
            basis_note=required_text(raw.get("basis_note", ""), "basis_note", 512)
            if raw.get("basis_note") is not None
            else "",
        )


@dataclass(frozen=True, slots=True)
class AgreementDraft:
    agreement_id: str
    achievement_id: str
    version_label: str
    effective_on: str
    expires_on: str | None
    basis_summary: str
    terms: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AgreementDraft":
        effective_on, expires_on = date_range(
            raw.get("effective_on"), raw.get("expires_on"), "effective_on", "expires_on"
        )
        label = required_text(raw.get("version_label"), "version_label", 32)
        terms_raw = raw.get("terms", {})
        if not isinstance(terms_raw, Mapping):
            raise ValidationFailed("terms 必须是对象")
        terms = dict(terms_raw)
        return cls(
            agreement_id=identifier(raw.get("agreement_id"), "agreement_id"),
            achievement_id=identifier(raw.get("achievement_id"), "achievement_id"),
            version_label=label,
            effective_on=effective_on,
            expires_on=expires_on,
            basis_summary=required_text(raw.get("basis_summary", ""), "basis_summary", 1024)
            if raw.get("basis_summary") is not None
            else "",
            terms=terms,
        )


@dataclass(frozen=True, slots=True)
class OwnershipTerm:
    entries: list[dict[str, Any]]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], field: str = "ownership") -> "OwnershipTerm":
        return cls(_ownership_entries(raw.get(field), field))


@dataclass(frozen=True, slots=True)
class RegionGrant:
    region_code: str
    field_scope: str

    @classmethod
    def from_value(cls, value: object, field: str) -> "RegionGrant":
        if isinstance(value, str):
            code, scope = value, "all"
        elif isinstance(value, Mapping):
            code = value.get("region_code", "")
            scope = value.get("field_scope", "all")
        else:
            raise ValidationFailed(f"{field} 必须是地域编码字符串或对象")
        code_text = required_text(code, f"{field}.region_code", 32).upper()
        scope_text = required_text(scope, f"{field}.field_scope", 32)
        return cls(code_text, scope_text)


def _region_list(raw: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ValidationFailed(f"{field} 必须是非空数组")
    grants = [RegionGrant.from_value(item, f"{field}[{index}]") for index, item in enumerate(raw)]
    keys = [(item.region_code, item.field_scope) for item in grants]
    if len(set(keys)) != len(keys):
        raise ValidationFailed(f"{field} 中存在重复的地域/领域范围")
    return [{"region_code": item.region_code, "field_scope": item.field_scope} for item in grants]


def _revenue_split(
    raw: Any, field: str, ownership: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if raw is None:
        # 默认按权属比例分配给各权属方。
        return [
            {
                "payee_id": item["owner_id"],
                "share_percent": normalize_share(Decimal(str(item["share_percent"]))),
                "source": "ownership_default",
            }
            for item in ownership
        ]
    if not isinstance(raw, list) or not raw:
        raise ValidationFailed(f"{field} 必须是非空数组")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"{field}[{index}] 必须是对象")
        payee = identifier(item.get("payee_id"), f"{field}[{index}].payee_id")
        if payee in seen:
            raise ValidationFailed(f"{field} 中收款方 {payee} 重复")
        seen.add(payee)
        share = share_value(item.get("share_percent"), f"{field}[{index}].share_percent")
        entries.append({"payee_id": payee, "share_percent": share, "source": "explicit"})
    total = sum((item["share_percent"] for item in entries), ZERO)
    if total <= ZERO or total > HUNDRED:
        raise ValidationFailed(f"{field} 比例之和必须在 0 到 100% 之间，当前为 {quantize_money(total)}%")
    return entries


@dataclass(frozen=True, slots=True)
class LicenseIssue:
    license_id: str
    agreement_id: str
    licensee_id: str
    license_type: str
    regions: list[dict[str, str]]
    valid_from: str
    valid_until: str | None
    field_scope_globally: str
    improvement_policy: str
    revenue_split: list[dict[str, Any]]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], ownership: list[dict[str, Any]]) -> "LicenseIssue":
        license_type = required_text(raw.get("license_type"), "license_type", 16)
        if license_type not in LICENSE_TYPES:
            raise ValidationFailed("license_type 必须是 exclusive、sole 或 non_exclusive")
        policy = required_text(raw.get("improvement_policy", "case_by_case"), "improvement_policy", 16)
        if policy not in IMPROVEMENT_OWNERSHIP:
            raise ValidationFailed("improvement_policy 取值不合法")
        valid_from, valid_until = date_range(
            raw.get("valid_from"), raw.get("valid_until"), "valid_from", "valid_until"
        )
        field_scope_globally = required_text(raw.get("field_scope", "all"), "field_scope", 32)
        regions = _region_list(raw.get("regions"), "regions")
        return cls(
            license_id=identifier(raw.get("license_id"), "license_id"),
            agreement_id=identifier(raw.get("agreement_id"), "agreement_id"),
            licensee_id=identifier(raw.get("licensee_id"), "licensee_id"),
            license_type=license_type,
            regions=regions,
            valid_from=valid_from,
            valid_until=valid_until,
            field_scope_globally=field_scope_globally,
            improvement_policy=policy,
            revenue_split=_revenue_split(raw.get("revenue_split"), "revenue_split", ownership),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ImprovementFiling:
    improvement_id: str
    parent_license_id: str
    title: str
    ownership: str
    owner_shares: list[dict[str, Any]]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ImprovementFiling":
        ownership = required_text(raw.get("ownership"), "ownership", 16)
        if ownership not in IMPROVEMENT_OWNERSHIP:
            raise ValidationFailed("ownership 取值不合法")
        shares_raw = raw.get("owner_shares")
        if ownership == "joint":
            shares = _ownership_entries(shares_raw, "owner_shares")
        else:
            if shares_raw:
                raise ValidationFailed(f"ownership 为 {ownership} 时不应提供 owner_shares")
            shares = []
        return cls(
            improvement_id=identifier(raw.get("improvement_id"), "improvement_id"),
            parent_license_id=identifier(raw.get("parent_license_id"), "parent_license_id"),
            title=required_text(raw.get("title"), "title"),
            ownership=ownership,
            owner_shares=shares,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class RevenueEstimate:
    installment_id: str
    license_id: str
    period_label: str
    gross_amount: Decimal
    currency: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RevenueEstimate":
        currency = required_text(raw.get("currency", "CNY"), "currency", 8).upper()
        return cls(
            installment_id=identifier(raw.get("installment_id"), "installment_id"),
            license_id=identifier(raw.get("license_id"), "license_id"),
            period_label=required_text(raw.get("period_label"), "period_label", 32),
            gross_amount=decimal_value(raw.get("gross_amount"), "gross_amount", minimum=Decimal("0")),
            currency=currency,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
