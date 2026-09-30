"""成果许可领域输入契约。

所有外部入站数据都在这里完成结构校验，业务规则校验放在 service 层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
OWNER_KINDS = {"university_team", "enterprise", "individual", "platform"}
GRANT_KINDS = {"non_exclusive", "exclusive", "sole"}
IMPROVEMENT_OWNERSHIP = {"licensee", "licensor", "joint", "follow_agreement"}
ROYALTY_TYPES = {"fixed", "sales_rate", "milestone", "tiered"}


def required_text(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field_name} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field_name} 格式不正确")
    return result


def optional_identifier(value: object, field_name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return identifier(value, field_name)


def decimal_value(
    value: object,
    field_name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field_name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field_name} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field_name} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field_name} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field_name} 不能大于 {maximum}")
    return result


def date_text(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 10)
    from datetime import date

    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field_name} 必须是 YYYY-MM-DD 日期") from exc


def ratio(value: object, field_name: str) -> Decimal:
    """0 到 1 之间的份额比例，最多 6 位小数。"""
    result = decimal_value(value, field_name, minimum=Decimal("0"), maximum=Decimal("1"))
    exponent = -result.as_tuple().exponent if isinstance(result.as_tuple().exponent, int) else 0
    if exponent > 6:
        raise ValidationFailed(f"{field_name} 小数位不能超过 6 位")
    return result


@dataclass(frozen=True, slots=True)
class OwnershipShareInput:
    owner_id: str
    owner_kind: str
    share: Decimal
    basis: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OwnershipShareInput":
        kind = required_text(raw.get("owner_kind"), "owner_kind", 24)
        if kind not in OWNER_KINDS:
            raise ValidationFailed("owner_kind 必须是 university_team、enterprise、individual 或 platform")
        return cls(
            owner_id=identifier(raw.get("owner_id"), "owner_id"),
            owner_kind=kind,
            share=ratio(raw.get("share"), "share"),
            basis=required_text(raw.get("basis"), "basis", 512),
        )


@dataclass(frozen=True, slots=True)
class AgreementInput:
    agreement_id: str
    achievement_id: str
    title: str
    version: str
    effective_date: str
    expiry_date: str
    supersedes_agreement_id: str | None
    ownership: tuple[OwnershipShareInput, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AgreementInput":
        agreement_id = identifier(raw.get("agreement_id"), "agreement_id")
        achievement_id = identifier(raw.get("achievement_id"), "achievement_id")
        version = required_text(raw.get("version"), "version", 32)
        effective_date = date_text(raw.get("effective_date"), "effective_date")
        expiry_date = date_text(raw.get("expiry_date"), "expiry_date")
        if expiry_date <= effective_date:
            raise ValidationFailed("expiry_date 必须晚于 effective_date")
        supersedes = optional_identifier(raw.get("supersedes_agreement_id"), "supersedes_agreement_id")
        if supersedes == agreement_id:
            raise ValidationFailed("协议不能替代自身")
        owners = raw.get("ownership")
        if not isinstance(owners, list) or not owners:
            raise ValidationFailed("ownership 必须是非空列表")
        ownership = tuple(OwnershipShareInput.from_dict(item) for item in owners)
        total = sum((item.share for item in ownership), Decimal("0"))
        if total != Decimal("1"):
            raise ValidationFailed(f"权属份额合计必须等于 1，当前为 {total}")
        owner_ids = [item.owner_id for item in ownership]
        if len(set(owner_ids)) != len(owner_ids):
            raise ValidationFailed("权属主体不能重复")
        return cls(
            agreement_id=agreement_id,
            achievement_id=achievement_id,
            title=required_text(raw.get("title"), "title"),
            version=version,
            effective_date=effective_date,
            expiry_date=expiry_date,
            supersedes_agreement_id=supersedes,
            ownership=ownership,
        )


@dataclass(frozen=True, slots=True)
class LicenseeInput:
    licensee_id: str
    name: str
    credit_code: str
    contact: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LicenseeInput":
        return cls(
            licensee_id=identifier(raw.get("licensee_id"), "licensee_id"),
            name=required_text(raw.get("name"), "name"),
            credit_code=required_text(raw.get("credit_code"), "credit_code", 64),
            contact=required_text(raw.get("contact"), "contact", 128),
        )


@dataclass(frozen=True, slots=True)
class TerritoryInput:
    region_code: str
    region_name: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TerritoryInput":
        return cls(
            region_code=identifier(raw.get("region_code"), "region_code"),
            region_name=required_text(raw.get("region_name"), "region_name", 128),
        )


@dataclass(frozen=True, slots=True)
class RoyaltyTermInput:
    royalty_type: str
    rate: Decimal | None
    fixed_amount: Decimal | None
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RoyaltyTermInput":
        royalty_type = required_text(raw.get("royalty_type"), "royalty_type", 24)
        if royalty_type not in ROYALTY_TYPES:
            raise ValidationFailed("royalty_type 必须是 fixed、sales_rate、milestone 或 tiered")
        rate = None
        if raw.get("rate") is not None:
            rate = ratio(raw.get("rate"), "rate")
        fixed_amount = None
        if raw.get("fixed_amount") is not None:
            fixed_amount = decimal_value(raw.get("fixed_amount"), "fixed_amount", minimum=Decimal("0"))
        return cls(
            royalty_type=royalty_type,
            rate=rate,
            fixed_amount=fixed_amount,
            note=required_text(raw.get("note", ""), "note", 256) if raw.get("note") else "",
        )


@dataclass(frozen=True, slots=True)
class GrantInput:
    grant_id: str
    agreement_id: str
    licensee_id: str
    grant_kind: str
    territories: tuple[TerritoryInput, ...]
    fields_of_use: str
    start_date: str
    end_date: str
    exclusive: bool
    royalty: RoyaltyTermInput

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GrantInput":
        grant_kind = required_text(raw.get("grant_kind"), "grant_kind", 24)
        if grant_kind not in GRANT_KINDS:
            raise ValidationFailed("grant_kind 必须是 non_exclusive、exclusive 或 sole")
        territories_raw = raw.get("territories")
        if not isinstance(territories_raw, list) or not territories_raw:
            raise ValidationFailed("territories 必须是非空列表")
        territories = tuple(TerritoryInput.from_dict(item) for item in territories_raw)
        codes = [item.region_code for item in territories]
        if len(set(codes)) != len(codes):
            raise ValidationFailed("同一授权内地域不能重复")
        start_date = date_text(raw.get("start_date"), "start_date")
        end_date = date_text(raw.get("end_date"), "end_date")
        if end_date <= start_date:
            raise ValidationFailed("end_date 必须晚于 start_date")
        royalty_raw = raw.get("royalty")
        if not isinstance(royalty_raw, Mapping):
            raise ValidationFailed("royalty 必须是对象")
        return cls(
            grant_id=identifier(raw.get("grant_id"), "grant_id"),
            agreement_id=identifier(raw.get("agreement_id"), "agreement_id"),
            licensee_id=identifier(raw.get("licensee_id"), "licensee_id"),
            grant_kind=grant_kind,
            territories=territories,
            fields_of_use=required_text(raw.get("fields_of_use"), "fields_of_use", 256),
            start_date=start_date,
            end_date=end_date,
            exclusive=grant_kind in {"exclusive", "sole"},
            royalty=RoyaltyTermInput.from_dict(royalty_raw),
        )


@dataclass(frozen=True, slots=True)
class ImprovementInput:
    improvement_id: str
    parent_achievement_id: str
    agreement_id: str
    title: str
    ownership_rule: str
    contributor_licensee_id: str | None
    derived_grant_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ImprovementInput":
        rule = required_text(raw.get("ownership_rule"), "ownership_rule", 24)
        if rule not in IMPROVEMENT_OWNERSHIP:
            raise ValidationFailed("ownership_rule 必须是 licensee、licensor、joint 或 follow_agreement")
        return cls(
            improvement_id=identifier(raw.get("improvement_id"), "improvement_id"),
            parent_achievement_id=identifier(raw.get("parent_achievement_id"), "parent_achievement_id"),
            agreement_id=identifier(raw.get("agreement_id"), "agreement_id"),
            title=required_text(raw.get("title"), "title"),
            ownership_rule=rule,
            contributor_licensee_id=optional_identifier(
                raw.get("contributor_licensee_id"), "contributor_licensee_id"
            ),
            derived_grant_id=optional_identifier(raw.get("derived_grant_id"), "derived_grant_id"),
        )
