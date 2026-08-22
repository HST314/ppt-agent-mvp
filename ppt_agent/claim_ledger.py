from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any

from .errors import ValidationError


_DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:[-/.年])\s*\d{1,2}(?:\s*(?:[-/.月])\s*\d{1,2}\s*日?)?(?!\d)")
_QUARTER = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:年\s*)?(?:Q[1-4]|第[一二三四1234]季度)", re.I)
_NUMBER = r"\d+(?:[\s,，]\d{3})*(?:\.\d+)?"
# One controlled unit vocabulary is shared by standalone metrics and both
# endpoints of metric transitions.  Longest/dimension-bearing spellings must
# come first so ``万人`` is never truncated to ``万`` and ``单/小时`` is not
# split into an unrelated number plus ``小时``.
_UNIT = (
    r"(?:单\s*[/／]\s*小时|件\s*[/／]\s*日|人\s*[/／]\s*天|人天|"
    r"%|％|(?:人民币)?亿元|(?:人民币)?万元|亿美元|万美元|亿元|万元|万人|万家|万条|万次|"
    r"百万\+?|亿\+?|万\+?(?![元人家条次])|美元|人民币|元|个\s*工作日|工作日|"
    r"毫秒|秒|分钟|小时|天|周|个月|月|年|倍|[×xX]|单|件|条|次|人|家|业务线)"
)
_METRIC = re.compile(rf"(?<![A-Za-z0-9]){_NUMBER}\s*{_UNIT}(?![A-Za-z])")
_TRANSITION_WORD = r"(?:→|⇒|->|至|到|提升(?:到|至)|提高(?:到|至)|增长(?:到|至)|增至|升至|下降(?:到|至)|降低(?:到|至)|降至|变为)"
_TRANSITION = re.compile(
    rf"(?<![A-Za-z0-9])(?P<before>{_NUMBER})\s*(?P<before_unit>{_UNIT})?\s*"
    rf"(?P<operator>{_TRANSITION_WORD})\s*(?P<after>{_NUMBER})\s*(?P<after_unit>{_UNIT})?(?![A-Za-z0-9])"
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
    normalized = re.sub(r"[\s,，]", "", value).replace("％", "%").replace("／", "/").casefold()
    transition = _TRANSITION.fullmatch(normalized)
    if transition:
        before_unit = transition.group("before_unit") or ""
        after_unit = transition.group("after_unit") or ""
        # A unit written once applies to both endpoints.  Explicitly different
        # dimensions are not collapsed into one relationship identity.
        if before_unit and after_unit and before_unit != after_unit:
            return normalized
        unit = before_unit or after_unit
        normalized = f"{transition.group('before')}{unit}→{transition.group('after')}{unit}"
    # Chinese budget copy routinely alternates between ``24万`` and
    # ``24万元``.  They are the same RMB magnitude; dimension-bearing forms
    # such as ``24万人`` remain distinct and therefore fail closed.
    normalized = re.sub(r"(?<=\d)人民币万元$", "万", normalized)
    normalized = re.sub(r"(?<=\d)万元$", "万", normalized)
    normalized = re.sub(r"(?<=\d)人民币亿元$", "亿", normalized)
    normalized = re.sub(r"(?<=\d)亿元$", "亿", normalized)
    return normalized


def _legacy_normalized(value: str) -> str:
    """The persisted v1 spelling before semantic currency normalization."""
    return re.sub(r"[\s,，]", "", value).replace("％", "%").casefold()


def _kind(pattern: re.Pattern[str]) -> str:
    return {
        _DATE: "date", _QUARTER: "quarter", _METRIC: "metric", _TRANSITION: "metric_transition", _FREQUENCY: "frequency",
        _LEGAL: "legal_commitment", _ORG: "organization_commitment",
    }[pattern]


def _occurrences(text: str) -> list[dict[str, Any]]:
    # Planning artifacts are Markdown.  Models commonly emphasize the two
    # sides of a transition independently (for example
    # ``**4.2** 提升至 **4.6**``).  Replace inline formatting delimiters with
    # same-length whitespace before scanning so semantic claims can span the
    # delimiters while every recorded offset still points into the original
    # artifact.  The visible value is reconstructed without those delimiters.
    scan_text = re.sub(r"[*_`]", " ", text)
    found = []
    for pattern in (_DATE, _QUARTER, _TRANSITION, _METRIC, _FREQUENCY, _LEGAL, _ORG):
        for match in pattern.finditer(scan_text):
            visible_value = re.sub(r"[*_`]", "", text[match.start():match.end()])
            if pattern is _TRANSITION:
                before_unit = re.sub(r"\s+", "", match.group("before_unit") or "").replace("／", "/")
                after_unit = re.sub(r"\s+", "", match.group("after_unit") or "").replace("／", "/")
                if before_unit and after_unit and before_unit != after_unit:
                    continue
            found.append({
                "kind": _kind(pattern),
                "value": visible_value,
                "normalized_value": _normalized(visible_value),
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
            or claim["normalized_value"] not in {_normalized(claim["value"]), _legacy_normalized(claim["value"])}
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


def audit_claims(
    text: str,
    ledger: dict[str, Any],
    *,
    required_claim_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    allow_disclosed_assumptions: bool = True,
) -> dict[str, Any]:
    validate_claim_ledger(ledger)
    # Recompute the semantic key from the immutable display value so ledgers
    # persisted before the currency-equivalence fix remain readable.
    known = {_normalized(claim["value"]): claim for claim in ledger["claims"]}
    known_ids = {claim["claim_id"] for claim in ledger["claims"]}
    required = set(required_claim_ids or ())
    if not required.issubset(known_ids):
        raise ValidationError("Claim Ledger required_claim_ids 包含未知 claim")
    bindings, unbound = [], []
    for occurrence in _occurrences(text):
        context = text[max(0, occurrence["start"] - 32): min(len(text), occurrence["end"] + 32)]
        if allow_disclosed_assumptions and _DISCLOSED.search(context):
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
    covered_ids = {
        claim_id
        for binding in bindings
        for claim_id in binding.get("source_claim_ids", [])
        if claim_id in known_ids
    }
    missing = [claim for claim in ledger["claims"] if claim["claim_id"] in required - covered_ids]
    return {
        "passed": not unbound and not missing,
        "bindings": bindings,
        "unbound": unbound,
        "binding_count": len(bindings),
        "unbound_count": len(unbound),
        "required_claim_ids": sorted(required),
        "required_count": len(required),
        "covered_required_claim_ids": sorted(required & covered_ids),
        "covered_required_count": len(required & covered_ids),
        "missing_required": missing,
        "missing_required_count": len(missing),
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


def audit_html_claims(
    html_text: str,
    ledger: dict[str, Any],
    *,
    required_claim_ids: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any]:
    parser = _VisibleText()
    parser.feed(html_text)
    parser.close()
    # A non-whitespace record separator preserves DOM/text-node boundaries.
    # Regexes may consume ordinary formatting whitespace inside one node, but
    # can no longer invent a metric by joining e.g. one node ending in ``0.4``
    # with the next node starting in ``人员``.
    return audit_claims("\n\u241e\n".join(parser.text), ledger, required_claim_ids=required_claim_ids)


def audit_html_claims_by_slide(
    html_by_slide: dict[str, str],
    ledger: dict[str, Any],
    required_claim_ids_by_slide: dict[str, list[str]],
) -> dict[str, Any]:
    """Audit each slide against the claims assigned by the frozen outline.

    Batch-wide coverage is insufficient for generation: a model can move a
    budget or duration to another page and still make the concatenated DOM
    appear complete.  This boundary keeps page placement authoritative while
    reusing the same visible-text rules that exclude styles, scripts, hidden
    nodes and metadata.
    """
    validate_claim_ledger(ledger)
    if set(html_by_slide) != set(required_claim_ids_by_slide):
        raise ValidationError("逐页 required claim 自检范围与页面片段不一致")
    known_ids = {claim["claim_id"] for claim in ledger["claims"]}
    pages: dict[str, dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    unbound: list[dict[str, Any]] = []
    required_count = 0
    covered_count = 0
    for slide_id, html_text in html_by_slide.items():
        required_ids = required_claim_ids_by_slide.get(slide_id)
        if not isinstance(required_ids, list) or len(required_ids) != len(set(required_ids)):
            raise ValidationError("逐页 required claim 映射格式无效或包含重复项")
        if not set(required_ids).issubset(known_ids):
            raise ValidationError("逐页 required claim 映射包含未知 claim")
        page = audit_html_claims(html_text, ledger, required_claim_ids=required_ids)
        pages[slide_id] = page
        required_count += page["required_count"]
        covered_count += page["covered_required_count"]
        missing.extend({"slide_id": slide_id, **claim} for claim in page["missing_required"])
        unbound.extend({"slide_id": slide_id, **claim} for claim in page["unbound"])
    return {
        "passed": not missing and not unbound,
        "pages": pages,
        "required_count": required_count,
        "covered_required_count": covered_count,
        "missing_required": missing,
        "missing_required_count": len(missing),
        "unbound": unbound,
        "unbound_count": len(unbound),
    }


def assert_claims_bound(
    text: str,
    ledger: dict[str, Any],
    stage: str,
    *,
    require_all: bool = False,
    allow_disclosed_assumptions: bool = True,
) -> dict[str, Any]:
    required = [claim["claim_id"] for claim in ledger["claims"]] if require_all else None
    result = audit_claims(
        text,
        ledger,
        required_claim_ids=required,
        allow_disclosed_assumptions=allow_disclosed_assumptions,
    )
    if result["unbound"]:
        values = "、".join(item["value"] for item in result["unbound"][:5])
        raise ValidationError(f"{stage} 包含未绑定事实：{values}")
    if result["missing_required"]:
        values = "、".join(item["value"] for item in result["missing_required"][:5])
        raise ValidationError(f"{stage} 遗漏必需事实：{values}")
    return result
