from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any

from .errors import ValidationError


_DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:[-/.年])\s*\d{1,2}(?:\s*(?:[-/.月])\s*\d{1,2}\s*日?)?(?!\d)")
_QUARTER = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:年\s*)?(?:Q[1-4]|第[一二三四1234]季度)", re.I)
_METRIC = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:[\s,，]\d{3})*(?:\.\d+)?\s*"
    r"(?:%|％|亿元|万元|百万\+?|亿\+?|万\+?|美元|人民币|元|个\s*工作日|工作日|"
    r"毫秒|秒|分钟|小时|天|周|个月|月|年|倍|[×xX]|条|次|人|家|业务线)(?![A-Za-z])"
)
_FREQUENCY = re.compile(r"(?:7\s*[×xX]\s*24|每(?:周|月|季度|年)|双周|会后\s*[一二三四五六七八九十两\d]+\s*(?:天|周|个工作日))")
_LEGAL = re.compile(r"(?:符合|遵守|满足)《[^》]{2,60}》")
_ORG = re.compile(r"(?:由|包含|组建|覆盖)[^。；;\n]{0,50}(?:法务|财务|运维|合规)[^。；;\n]{0,30}(?:代表|团队|小组|部门)")
_DISCLOSED = re.compile(r"(?:数据)?待(?:补充|确认|核实|定)|暂无数据|尚未提供|假设|建议|示例")
_FORMULA = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?P<unit>亿元|万元|元|%|％|周|月|天)?\s*\+\s*"
    r"(?P<b>\d+(?:\.\d+)?)\s*(?P=unit)?\s*=\s*(?P<c>\d+(?:\.\d+)?)\s*(?P=unit)?"
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _normalized(value: str) -> str:
    return re.sub(r"[\s,，]", "", value).replace("％", "%").casefold()


def _kind(pattern: re.Pattern[str]) -> str:
    return {
        _DATE: "date", _QUARTER: "quarter", _METRIC: "metric", _FREQUENCY: "frequency",
        _LEGAL: "legal_commitment", _ORG: "organization_commitment",
    }[pattern]


def _occurrences(text: str) -> list[dict[str, Any]]:
    found = []
    for pattern in (_DATE, _QUARTER, _METRIC, _FREQUENCY, _LEGAL, _ORG):
        for match in pattern.finditer(text):
            found.append({
                "kind": _kind(pattern),
                "value": match.group(0),
                "normalized_value": _normalized(match.group(0)),
                "start": match.start(),
                "end": match.end(),
            })
    unique = {}
    for item in sorted(found, key=lambda value: (value["start"], -(value["end"] - value["start"]))):
        key = (item["start"], item["end"], item["normalized_value"])
        unique.setdefault(key, item)
    return list(unique.values())


def build_claim_ledger(*, task_id: str, input_snapshot_hash: str, source_binding: Any, created_at: str) -> dict[str, Any]:
    source_text = json.dumps(source_binding, ensure_ascii=False, sort_keys=True) if not isinstance(source_binding, str) else source_binding
    claims = []
    for item in _occurrences(source_text):
        identity = f"{item['kind']}\0{item['normalized_value']}"
        claim_id = f"claim-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
        if any(existing["claim_id"] == claim_id for existing in claims):
            continue
        claims.append({
            "claim_id": claim_id,
            "kind": item["kind"],
            "value": item["value"],
            "normalized_value": item["normalized_value"],
            "status": "confirmed",
            "source_path": "frozen_input",
            "source_hash": hashlib.sha256(source_text.encode()).hexdigest(),
        })
    seed = {"task_id": task_id, "input_snapshot_hash": input_snapshot_hash, "claims": claims}
    return {
        "ledger_id": f"ledger-{hashlib.sha256(_canonical(seed)).hexdigest()[:20]}",
        **seed,
        "created_at": created_at,
        "schema_version": "1.0",
    }


def validate_claim_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    required = {"ledger_id", "task_id", "input_snapshot_hash", "claims", "created_at", "schema_version"}
    if not isinstance(ledger, dict) or set(ledger) != required or ledger.get("schema_version") != "1.0":
        raise ValidationError("Claim Ledger 结构无效")
    if (
        not isinstance(ledger.get("task_id"), str) or not ledger["task_id"]
        or not isinstance(ledger.get("created_at"), str) or not ledger["created_at"]
        or not isinstance(ledger.get("input_snapshot_hash"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", ledger["input_snapshot_hash"])
    ):
        raise ValidationError("Claim Ledger 标识或哈希无效")
    if not isinstance(ledger.get("claims"), list):
        raise ValidationError("Claim Ledger claims 必须是数组")
    seen = set()
    for claim in ledger["claims"]:
        if not isinstance(claim, dict) or set(claim) != {
            "claim_id", "kind", "value", "normalized_value", "status", "source_path", "source_hash",
        }:
            raise ValidationError("Claim Ledger claim 字段无效")
        if any(not isinstance(claim.get(name), str) or not claim[name]
               for name in ("claim_id", "kind", "value", "normalized_value", "status", "source_path", "source_hash")):
            raise ValidationError("Claim Ledger claim 字段类型无效")
        if claim["claim_id"] in seen or claim["status"] not in {"confirmed", "derived"}:
            raise ValidationError("Claim Ledger claim 重复或状态无效")
        identity = f"{claim['kind']}\0{claim['normalized_value']}"
        expected_claim_id = f"claim-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
        if (
            claim["claim_id"] != expected_claim_id
            or claim["normalized_value"] != _normalized(claim["value"])
            or not re.fullmatch(r"[0-9a-f]{64}", claim["source_hash"])
            or claim["source_path"] != "frozen_input"
        ):
            raise ValidationError("Claim Ledger claim 内容与标识不一致")
        seen.add(claim["claim_id"])
    seed = {"task_id": ledger["task_id"], "input_snapshot_hash": ledger["input_snapshot_hash"], "claims": ledger["claims"]}
    expected_ledger_id = f"ledger-{hashlib.sha256(_canonical(seed)).hexdigest()[:20]}"
    if ledger["ledger_id"] != expected_ledger_id:
        raise ValidationError("Claim Ledger 内容与标识不一致")
    return ledger


def _derived_binding(text: str, occurrence: dict[str, Any], known: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    window = text[max(0, occurrence["start"] - 48): min(len(text), occurrence["end"] + 48)]
    for formula in _FORMULA.finditer(window):
        unit = _normalized(formula.group("unit") or "")
        a = _normalized(formula.group("a") + unit)
        b = _normalized(formula.group("b") + unit)
        c = _normalized(formula.group("c") + unit)
        if occurrence["normalized_value"] != c or a not in known or b not in known:
            continue
        if abs(float(formula.group("a")) + float(formula.group("b")) - float(formula.group("c"))) > 1e-9:
            continue
        return {
            "status": "derived",
            "source_claim_ids": [known[a]["claim_id"], known[b]["claim_id"]],
            "formula": formula.group(0),
        }
    return None


def audit_claims(text: str, ledger: dict[str, Any]) -> dict[str, Any]:
    validate_claim_ledger(ledger)
    known = {claim["normalized_value"]: claim for claim in ledger["claims"]}
    bindings, unbound = [], []
    for occurrence in _occurrences(text):
        context = text[max(0, occurrence["start"] - 32): min(len(text), occurrence["end"] + 32)]
        if _DISCLOSED.search(context):
            bindings.append({**occurrence, "status": "disclosed_assumption", "source_claim_ids": [], "formula": ""})
            continue
        claim = known.get(occurrence["normalized_value"])
        if claim:
            bindings.append({**occurrence, "status": "bound", "source_claim_ids": [claim["claim_id"]], "formula": ""})
            continue
        derived = _derived_binding(text, occurrence, known)
        if derived:
            bindings.append({**occurrence, **derived})
            continue
        unbound.append(occurrence)
    return {
        "passed": not unbound,
        "bindings": bindings,
        "unbound": unbound,
        "binding_count": len(bindings),
        "unbound_count": len(unbound),
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
    }


class _VisibleText(HTMLParser):
    _SKIP = {"head", "style", "script", "template", "noscript", "title"}
    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {name.lower(): value or "" for name, value in attrs}
        hidden = (self.stack[-1][1] if self.stack else False) or tag in self._SKIP or "hidden" in values or values.get("aria-hidden", "").lower() == "true"
        if tag not in self._VOID:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        target = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == target:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.stack and not self.stack[-1][1] and data.strip():
            self.text.append(re.sub(r"\s+", " ", data).strip())


def audit_html_claims(html_text: str, ledger: dict[str, Any]) -> dict[str, Any]:
    parser = _VisibleText()
    parser.feed(html_text)
    parser.close()
    return audit_claims("\n".join(parser.text), ledger)


def assert_claims_bound(text: str, ledger: dict[str, Any], stage: str) -> dict[str, Any]:
    result = audit_claims(text, ledger)
    if result["unbound"]:
        values = "、".join(item["value"] for item in result["unbound"][:5])
        raise ValidationError(f"{stage} 包含未绑定事实：{values}")
    return result
