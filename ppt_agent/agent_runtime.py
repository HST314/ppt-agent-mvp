from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .errors import GatewayError, ValidationError
from .design_contract import validate_design_intent, validate_shared_design_assets
from .skill_runtime import SkillRuntime


STAGES = {"clarification", "narrative", "outline", "sample", "deck", "inspection"}
SKILL_STAGES = frozenset(STAGES - {"clarification"})
RENDERING_STAGES = frozenset({"sample", "deck"})
SKILL_ENTRY = "SKILL.md"
DATA_IMAGE = "data:image/"
STAGE_PROMPTS = {
    "clarification": (
        "直接依据原始任务卡、规范化结果、资源摘要和澄清上下文（当前轮次、提问预算、已答记录与风格指令）提出澄清问题；"
        "不得重复询问已知事实或已答记录中已覆盖的内容。每个问题必须包含稳定 question_id、目标 field_path、明确 prompt、"
        "helper_text、0 个或多个带 value/label/description 的 options、allow_other 与 blocking。"
        "本阶段不提供也不需要任何 Skill 工具，禁止请求工具。"
    ),
    "narrative": (
        "根据任务卡生成叙事结构 Markdown；不要生成逐页 HTML。数字、日期、实体、效果与承诺只能来自冻结任务卡，"
        "不得把计划改写成已发生事实，不得补造 SLA/KPI/倍数/期限，也不得输出 XX、TBD、[必填]、X 月 X 日等占位符。"
        "输入中的 narrative_numeric_policy 是量化事实硬约束：只能使用 allowed_claims 中已有的量化值；"
        "总周期不得拆成未绑定的阶段周数或累计周数，已有比例不得拆分、重配或补造新占比。"
        "需要表达阶段时使用不带数字的阶段名称；不得用‘假设/建议/待确认’包装自造数字来绕过约束。"
        "输入中的 narrative_structure_policy 是最低语义与结构硬约束：必须输出完整 Markdown，并把 required_context 中"
        "任务主题、目标和受众的 value 分别逐字写入正文，不得缩写、改写或省略；用至少两个有实质正文的二级章节表达"
        "核心论点与页面推进逻辑；不得返回分析请求、待办或元说明。若输入包含 semantic_correction，必须逐字复制其中"
        "required_context_verbatim 的每个 value 后再提交完整叙事。"
        "先完整读取当前 Skill 的 SKILL.md，再按其中指引按需读取必要资源；不要一次性读取整个 Skill，也不要重复读取。"
    ),
    "outline": (
        "根据已确认叙事生成结构化逐页大纲；不要自行编写页面 ID，也不要返回 markdown 字段。"
        "每页必须只包含 title、purpose、content_markdown、resource_uris；resource_uris 只能选自输入的冻结资源清单，"
        "没有合适资源时返回空数组。content_markdown 只写页内正文或列表，不得包含一级、二级标题。"
        "例如：{\"slides\":[{\"title\":\"开场与目标\",\"purpose\":\"建立共同目标\","
        "\"content_markdown\":\"- 背景\\n- 目标\",\"resource_uris\":[]}]}}。"
        "所有事实、数字、日期、实体与承诺必须能在冻结任务卡中找到依据；不得沿用叙事中的无依据新增值，"
        "不得输出 XX、TBD、[必填]、X 月 X 日等占位符。"
        "若输入包含 semantic_correction，须依据其中的具体错误修正并重新提交完整 slides。"
        "先完整读取当前 Skill 的 SKILL.md，再按其中指引按需读取必要资源；读取已经足够时立即提交大纲 JSON，不要重复读取。"
    ),
    "sample": (
        "只为外层状态机指定的代表页生成 section 片段，不得扩展到全稿。先依据已读取的当前 Skill 与页面内容形成"
        "DesignIntent，说明风格、配色、排版、布局原则及理由，并把跨页复用的 CSS 放入 shared_assets.css。"
        "PresentationTechnicalContract 只规定画布、页面 ID、资源、安全与交付边界，不规定风格、class 或布局。"
        "required_claims_by_slide 中每个 value 必须逐字出现在对应页面的可见正文。每项 html 必须是单个 class 包含 slide、"
        "id 与 data-slide-id 均等于给定 ID 的 section；不要生成 html/head/body/style/script。可主动调用 Skill 自检工具并"
        "自行修正审美问题，其建议不属于框架门禁。若输入包含 technical_correction，只修复其中明确的 Schema、页面集合、"
        "资源、安全或渲染错误后重新提交完整结果。"
    ),
    "deck": (
        "只为外层状态机给定的未确认页面生成 section 片段，不得重做确认样品或生成公共 shell。必须复用输入中的"
        "confirmed_design_intent 与 confirmed_shared_assets，保持样品已确认的配色、排版、间距、组件语言与共享 CSS；"
        "不要重新探索另一套设计语言。PresentationTechnicalContract 只规定通用技术边界。required_claims_by_slide 中"
        "每个 value 必须逐字出现在对应页面的可见正文。每项 html 必须是单个 class 包含 slide、id 与 data-slide-id 均"
        "等于给定 ID 的 section；不要生成 html/head/body/style/script。若输入包含 technical_correction，只修复其中明确"
        "的 Schema、页面集合、资源、安全或渲染错误后重新提交完整结果。"
    ),
    "inspection": "独立检查大纲与 HTML；必须逐项应用检查清单并结合 browser_evidence 中的 Chromium 渲染测量，仅报告有证据的问题，不得直接修改产物。浏览器证据不可用或包含问题时不得返回 passed=true。",
}


def _object_schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_DESIGN_INTENT_SCHEMA = _object_schema({
    "style_summary": {"type": "string", "minLength": 1},
    "color_strategy": {"type": "string", "minLength": 1},
    "typography_strategy": {"type": "string", "minLength": 1},
    "layout_principles": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
    "rationale": {"type": "string", "minLength": 1},
}, ["style_summary", "color_strategy", "typography_strategy", "layout_principles", "rationale"])
_SHARED_ASSETS_SCHEMA = _object_schema({"css": {"type": "string"}}, ["css"])
_SLIDES_SCHEMA = {"type": "array", "minItems": 1, "items": _object_schema({
    "slide_id": {"type": "string"}, "html": {"type": "string"},
}, ["slide_id", "html"])}
_RENDERING_SCHEMA = _object_schema({
    "slides": _SLIDES_SCHEMA,
    "design_intent": _DESIGN_INTENT_SCHEMA,
    "shared_assets": _SHARED_ASSETS_SCHEMA,
}, ["slides", "design_intent", "shared_assets"])


# These schemas are the complete local contract.  They intentionally include
# defensive constraints that are not accepted by every provider's strict JSON
# Schema subset; AgentRuntime always revalidates the completed response against
# this contract even when the provider performed its own validation.
STAGE_OUTPUT_SCHEMAS = {
    "clarification": {"name": "clarification", "strict": True, "schema": _object_schema({"questions": {"type": "array", "items": _object_schema({
        "question_id": {"type": "string"}, "field_path": {"type": "string"}, "prompt": {"type": "string"},
        "helper_text": {"type": "string"}, "options": {"type": "array", "items": _object_schema({
            "value": {"type": "string"}, "label": {"type": "string"}, "description": {"type": "string"}
        }, ["value", "label", "description"])}, "recommended": {"type": "string"}, "allow_other": {"type": "boolean"}, "blocking": {"type": "boolean"}
    }, ["question_id", "field_path", "prompt", "helper_text", "options", "allow_other", "blocking"])}}, ["questions"])},
    "narrative": {"name": "narrative", "strict": True, "schema": _object_schema({"markdown": {"type": "string", "minLength": 1, "pattern": r"\S"}}, ["markdown"])},
    "outline": {"name": "outline", "strict": True, "schema": _object_schema({"slides": {
        "type": "array", "minItems": 1, "items": _object_schema({
            "title": {"type": "string", "minLength": 1},
            "purpose": {"type": "string", "minLength": 1},
            "content_markdown": {"type": "string", "minLength": 1},
            "resource_uris": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        }, ["title", "purpose", "content_markdown", "resource_uris"]),
    }}, ["slides"])},
    "sample": {"name": "sample_slides", "strict": True, "schema": _RENDERING_SCHEMA},
    "deck": {"name": "deck_slides", "strict": True, "schema": _RENDERING_SCHEMA},
    "inspection": {"name": "inspection", "strict": True, "schema": _object_schema({"passed": {"type": "boolean"}, "issues": {"type": "array", "items": _object_schema({
        "issue_id": {"type": "string"}, "severity": {"type": "string", "enum": ["warning", "blocker"]},
        "level": {"type": "string", "enum": ["element", "slide", "deck"]}, "code": {"type": "string"},
        "message": {"type": "string"}, "slide_id": {"type": "string"}, "element_id": {"type": "string"},
        "evidence": {"type": "string"}, "suggestion": {"type": "string"},
    }, ["issue_id", "severity", "level", "code", "message", "slide_id", "element_id", "evidence", "suggestion"])}}, ["passed", "issues"])},
}


_PROVIDER_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({"minLength", "minItems", "pattern", "uniqueItems"})


def _provider_schema(value: Any) -> Any:
    """Return the strict provider subset without weakening local validation."""
    if isinstance(value, dict):
        return {
            key: _provider_schema(item)
            for key, item in value.items()
            if key not in _PROVIDER_UNSUPPORTED_SCHEMA_KEYWORDS
        }
    if isinstance(value, list):
        return [_provider_schema(item) for item in value]
    return value


STAGE_PROVIDER_SCHEMAS = {
    stage: _provider_schema(schema)
    for stage, schema in STAGE_OUTPUT_SCHEMAS.items()
}
PRODUCT_OVERRIDE = """产品规则高于 Skill：你只处理当前阶段，不得推进工作流或请求状态机操作。
仅允许纯文本输入；禁止联网、图片输入、图片生成、Shell、文件写入、自更新和安装依赖。
只能使用请求中明确提供的 Skill 工具；其中脚本在隔离临时目录执行，结果仅是自检建议，不是交付门禁。
不要把整个 Skill 一次性读入。最终仅返回符合指定 JSON Schema 的 JSON。"""
CLARIFICATION_OVERRIDE = """产品规则高于 Skill：你只处理澄清阶段，不得推进工作流或请求状态机操作。
仅允许纯文本输入；禁止联网、图片输入、图片生成、Shell、文件读写、自更新和安装依赖。
当前请求没有可用工具；直接依据输入作答。最终仅返回符合指定 JSON Schema 的 JSON。"""


def _output_contract(response_schema: dict) -> str:
    """Spell the stage schema out in the prompt.

    Providers that ignore ``text.format`` (or run with ``structured_output:
    prompt``) get no enforcement from the endpoint; naming the exact shape in
    the prompt is what keeps their final output parseable.
    """
    schema = response_schema.get("schema", response_schema)
    return (
        "输出契约：最终答案必须且只能是符合以下 JSON Schema 的 JSON object；"
        "不得包含任何额外字段、解释文字或 markdown 代码围栏。\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def _extract_json_object(text: str) -> Any:
    """Parse model output as JSON, tolerating code fences and surrounding prose."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    fence = re.match(r"^```[A-Za-z]*\s*(?P<body>.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        return json.loads(fence.group("body"))
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        return json.loads(stripped[start : end + 1])
    return json.loads(stripped)


def normalize_rendering_output(value: dict, expected_slide_ids: list[str]) -> dict:
    """Validate and normalize the HTML fragment contract for sample/deck output."""
    slides = value.get("slides")
    if not expected_slide_ids or len(set(expected_slide_ids)) != len(expected_slide_ids):
        raise GatewayError("渲染请求的页面 ID 无效")
    if not isinstance(slides, list) or [item.get("slide_id") for item in slides if isinstance(item, dict)] != expected_slide_ids:
        raise GatewayError("模型页面片段与请求页面不一致")
    normalized = []
    for index, item in enumerate(slides):
        fragment_field = "html_fragment" if isinstance(item, dict) and "html_fragment" in item else "html"
        fragment = item.get(fragment_field)
        if not isinstance(fragment, str) or not fragment.strip():
            raise GatewayError(f"output.slides[{index}].{fragment_field} 不能为空")
        fragment = fragment.strip()
        if fragment.startswith("```"):
            fragment = re.sub(r"^```[a-zA-Z]*\s*", "", fragment)
            fragment = re.sub(r"\s*```$", "", fragment).strip()
        tag_match = re.fullmatch(r'<section\b(?P<attrs>[^>]*)>[\s\S]*</section>', fragment, re.I)
        if not tag_match:
            raise GatewayError(f"output.slides[{index}].html 必须是单个完整 section 片段")
        attributes = tag_match.group("attrs")
        id_match = re.search(r'\bid=["\']([A-Za-z0-9_-]+)["\']', attributes, re.I)
        data_id_match = re.search(r'\bdata-slide-id=["\']([A-Za-z0-9_-]+)["\']', attributes, re.I)
        class_match = re.search(r'\bclass=["\']([^"\']*)["\']', attributes, re.I)
        if not class_match or "slide" not in class_match.group(1).split():
            raise GatewayError(f"output.slides[{index}].html 的 section 必须包含 slide class")
        slide_id = item["slide_id"]
        declared_ids = [match.group(1) for match in (id_match, data_id_match) if match is not None]
        if any(declared != slide_id for declared in declared_ids):
            raise GatewayError(f"output.slides[{index}].html 的页面 ID 与 slide_id 不一致")
        if not id_match:
            fragment = re.sub(r'^<section\b', f'<section id="{slide_id}"', fragment, count=1, flags=re.I)
        if not data_id_match:
            fragment = re.sub(r'^<section\b', f'<section data-slide-id="{slide_id}"', fragment, count=1, flags=re.I)
        normalized.append({**item, fragment_field: fragment})
    uses_html_fragment = any(isinstance(item, dict) and "html_fragment" in item for item in slides)
    if uses_html_fragment:
        result = {**value, "slides": normalized}
        if "design_intent" in value:
            result["design_intent"] = validate_design_intent(value.get("design_intent"))
        return result
    return {
        "slides": normalized,
        "design_intent": validate_design_intent(value.get("design_intent")),
        "shared_assets": validate_shared_design_assets(value.get("shared_assets")),
    }


def _list_skill_files_tool() -> dict:
    return {"type": "function", "name": "list_skill_files", "description": "列出当前 Skill 快照中的标准文件路径；只用于按需发现，不会读取文件正文", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}


def _read_skill_file_tool(
    paths: frozenset[str] | set[str] | None = None,
    *,
    paginated: bool = True,
) -> dict:
    path_schema: dict[str, Any] = {"type": "string"}
    if paths is not None:
        path_schema["enum"] = sorted(paths)
    properties: dict[str, Any] = {"path": path_schema}
    if paginated:
        properties.update({
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        })
    return {"type": "function", "name": "read_skill_file", "description": "读取一个当前 Skill 快照中的 UTF-8 文本文件；大文件可用 offset/limit 分段读取，必须首先完整读取 SKILL.md", "parameters": {"type": "object", "properties": properties, "required": ["path"], "additionalProperties": False}}


def _get_asset_info_tool(paths: frozenset[str] | set[str] | None = None) -> dict:
    path_schema: dict[str, Any] = {"type": "string"}
    if paths is not None:
        path_schema["enum"] = sorted(paths)
    return {"type": "function", "name": "get_asset_info", "description": "读取一个 Skill asset 的元数据，不读取二进制正文", "parameters": {"type": "object", "properties": {"path": path_schema}, "required": ["path"], "additionalProperties": False}}


def _run_skill_script_tool(paths: frozenset[str] | set[str] | None = None) -> dict:
    path_schema: dict[str, Any] = {"type": "string"}
    if paths is not None:
        path_schema["enum"] = sorted(paths)
    return {
        "type": "function",
        "name": "run_skill_script",
        "description": "可选运行一个 Skill 自检脚本；无网络、无模型凭据、只能写临时目录，失败或超时仅返回 advisory",
        "parameters": {
            "type": "object",
            "properties": {
                "path": path_schema,
                "args": {"type": "array", "items": {"type": "string"}},
                "stdin": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }


TOOLS = [_list_skill_files_tool(), _read_skill_file_tool(), _get_asset_info_tool(), _run_skill_script_tool()]


def _tools_for_stage(
    stage: str,
    skill: SkillRuntime,
    *,
    entry_read: bool,
    remaining_text_files: frozenset[str] | None = None,
) -> list[dict]:
    if stage == "clarification":
        return []
    if not entry_read:
        return [_read_skill_file_tool(frozenset({SKILL_ENTRY}), paginated=False)]
    text_files = remaining_text_files
    if text_files is None:
        text_files = frozenset(
            path for path in skill.manifest
            if PurePosixPath(path).suffix.lower() in skill.TEXT_SUFFIXES
        )
    assets = frozenset(path for path in skill.manifest if path.startswith("assets/"))
    scripts = frozenset(
        path for path in skill.manifest
        if path.startswith("scripts/") and PurePosixPath(path).suffix.lower() in skill.SCRIPT_SUFFIXES
    )
    return [
        _list_skill_files_tool(),
        *([_read_skill_file_tool(text_files)] if text_files else []),
        *([_get_asset_info_tool(assets)] if assets else []),
        *([_run_skill_script_tool(scripts)] if scripts else []),
    ]


@dataclass(frozen=True)
class AgentResult:
    value: dict
    audit: tuple[dict, ...]
    response_id: str | None
    validation: dict[str, Any] | None = None


class AgentRuntime:
    def __init__(self, client, skill: SkillRuntime, *, max_steps: int = 12, timeout_seconds: float = 60, clock=time.monotonic, max_output_bytes: int = 1024 * 1024, max_tool_calls: int = 24, max_provider_calls: int = 8, max_schema_corrections: int = 1, max_tool_error_rounds: int = 2, max_exploration_rounds: int | None = None, max_unique_files: int = 4, max_skill_bytes: int = 128 * 1024, reserved_final_calls: int = 1, allow_schema_override: bool = False):
        self.client, self.skill = client, skill
        self.max_steps, self.timeout_seconds, self.clock = max_steps, timeout_seconds, clock
        self.max_output_bytes, self.max_tool_calls = max_output_bytes, max_tool_calls
        if isinstance(max_provider_calls, bool) or not isinstance(max_provider_calls, int) or not 1 <= max_provider_calls <= 20:
            raise ValidationError("模型真实请求上限必须是 1 到 20 的整数")
        self.max_provider_calls = max_provider_calls
        if isinstance(max_schema_corrections, bool) or not isinstance(max_schema_corrections, int) or not 0 <= max_schema_corrections <= 2:
            raise ValidationError("Schema 纠错次数必须是 0 到 2 的整数")
        if isinstance(max_tool_error_rounds, bool) or not isinstance(max_tool_error_rounds, int) or not 1 <= max_tool_error_rounds <= 3:
            raise ValidationError("工具错误轮次必须是 1 到 3 的整数")
        self.max_schema_corrections = max_schema_corrections
        self.max_tool_error_rounds = max_tool_error_rounds
        if max_exploration_rounds is None:
            max_exploration_rounds = min(3, max_provider_calls - reserved_final_calls)
        limits = {
            "探索轮次": (max_exploration_rounds, 0, 10),
            "唯一文件数": (max_unique_files, 1, 20),
            "Skill 字节数": (max_skill_bytes, 1024, 512 * 1024),
            "最终输出预留请求数": (reserved_final_calls, 1, max_provider_calls - 1),
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum
            for value, minimum, maximum in limits.values()
        ):
            raise ValidationError("Agent 探索或最终输出预算无效")
        if max_exploration_rounds > max_provider_calls - reserved_final_calls:
            raise ValidationError("Agent 探索预算挤占了最终输出预留请求")
        self.max_exploration_rounds = max_exploration_rounds
        self.max_unique_files = max_unique_files
        self.max_skill_bytes = max_skill_bytes
        self.reserved_final_calls = reserved_final_calls
        self.allow_schema_override = allow_schema_override
        self.skill.max_total_bytes = min(self.skill.max_total_bytes, max_skill_bytes)
        self.last_audit: tuple[dict, ...] = ()

    def run(
        self,
        stage: str,
        payload: dict,
        *,
        response_schema: dict | None = None,
        capability_probe: bool = False,
        system_instruction: str | None = None,
        result_validator=None,
        max_semantic_corrections: int = 1,
    ) -> AgentResult:
        from .execution import checkpoint, interruptible, progress, remaining_seconds
        from .model_clients import ProviderCallBudget
        if stage not in STAGES:
            raise ValidationError("Agent 阶段不在允许列表")
        allowed_schemas = (STAGE_OUTPUT_SCHEMAS[stage], STAGE_PROVIDER_SCHEMAS[stage])
        if response_schema is not None and response_schema not in allowed_schemas and not self.allow_schema_override:
            raise ValidationError("阶段输出 Schema 不允许覆盖")
        local_schema = response_schema or STAGE_OUTPUT_SCHEMAS[stage]
        provider_schema = _provider_schema(local_schema)
        if (
            isinstance(max_semantic_corrections, bool)
            or not isinstance(max_semantic_corrections, int)
            or not 0 <= max_semantic_corrections <= 2
        ):
            raise ValidationError("语义纠错次数必须是 0 到 2 的整数")
        if system_instruction is not None and (
            not isinstance(system_instruction, str) or not system_instruction.strip()
        ):
            raise ValidationError("阶段补充指令无效")
        if not isinstance(payload, dict):
            raise ValidationError("Agent 输入无效")
        payload = self._text_only(payload)
        available_text_files = frozenset(
            name
            for name in self.skill.manifest
            if PurePosixPath(name).suffix.lower() in self.skill.TEXT_SUFFIXES
        )
        if stage in SKILL_STAGES and SKILL_ENTRY not in available_text_files:
            raise ValidationError("当前 Skill 快照缺少 UTF-8 入口文件 SKILL.md")
        allowed_skill_files = frozenset(self.skill.manifest)
        stage_tools = _tools_for_stage(stage, self.skill, entry_read=stage == "clarification")
        override = CLARIFICATION_OVERRIDE if stage == "clarification" else PRODUCT_OVERRIDE
        started, audit, tool_count, tool_error_rounds, schema_corrections = self.clock(), [], 0, 0, 0
        semantic_corrections = 0
        semantic_evidence = None
        active_step = 0

        def metrics(*, provider_calls: int | None = None) -> dict[str, Any]:
            return {
                "stage": stage,
                "agent_step": active_step,
                "max_steps": self.max_steps,
                "provider_calls": provider_call_budget.claimed if provider_calls is None else provider_calls,
                "max_provider_calls": self.max_provider_calls,
                "tool_calls": tool_count,
                "max_tool_calls": self.max_tool_calls,
            }

        def provider_claimed(claimed: int, limit: int) -> None:
            progress(
                "provider_request",
                f"模型请求 {claimed} / {limit}",
                metrics(provider_calls=claimed),
            )

        provider_call_budget = ProviderCallBudget(self.max_provider_calls, provider_claimed)
        successful_read_count, successful_read_paths, completed_read_paths = 0, set(), set()
        successful_read_keys: set[tuple[str, int, int | None]] = set()
        successful_read_digests: dict[str, str] = {}
        exploration_rounds, repeated_read_count, skill_bytes = 0, 0, 0
        force_final_output = False
        tool_error_corrections, recovery_active = 0, False
        entry_protocol_corrections = 0
        remaining_paths: frozenset[str] | None = None
        input_json=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        tool_contract = self._stage_tool_contract(stage, self.skill)
        tool_schema_contract = json.dumps(stage_tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        audit.append({"event": "run", "stage": stage, "skill": self.skill.skill_name, "skill_description_sha256": hashlib.sha256(self.skill.skill_description.encode()).hexdigest(), "skill_version": self.skill.skill_version, "skill_snapshot_sha256": self.skill.snapshot.digest, "input_sha256": hashlib.sha256(input_json.encode()).hexdigest(), "config_sha256": hashlib.sha256((STAGE_PROMPTS[stage]+override+tool_contract+tool_schema_contract).encode()).hexdigest(), "skill_entry": SKILL_ENTRY if stage in SKILL_STAGES else None, "max_exploration_rounds": self.max_exploration_rounds, "max_unique_files": self.max_unique_files, "max_skill_bytes": self.max_skill_bytes, "reserved_final_calls": self.reserved_final_calls})
        def probe_phase(reason: str) -> str:
            if stage == "clarification":
                return "strict_json_schema"
            if tool_count == 0:
                return "tool_request"
            if reason == "invalid_output":
                return "tool_final_output"
            return "tool_result"

        def fail(message: str, reason: str, cause=None, rejected_output=None):
            phase = probe_phase(reason) if capability_probe else None
            terminal = {
                "event": "terminal",
                "reason": reason,
                "tool_calls": tool_count,
                "provider_calls": provider_call_budget.claimed,
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
                "max_provider_calls": self.max_provider_calls,
                "exploration_rounds": exploration_rounds,
                "cumulative_skill_bytes": skill_bytes,
                "unique_skill_files": len(successful_read_paths),
                "applied_skill_files": sorted(successful_read_paths),
                "skill_entry_read": SKILL_ENTRY in successful_read_paths,
                "repeated_skill_reads": repeated_read_count,
            }
            if phase:
                terminal["probe_phase"] = phase
            audit.append(terminal)
            self.last_audit = tuple(audit)
            mapped_code = self._failure_code(reason, capability_probe, tool_count)
            underlying_code = getattr(cause, "code", None)
            error = GatewayError(
                message,
                code=mapped_code,
                probe_phase=phase,
                terminal_reason=reason if capability_probe else None,
                tool_calls=tool_count if capability_probe else None,
                underlying_code=underlying_code if underlying_code != mapped_code else None,
            )
            error.audit = self.last_audit
            if rejected_output is not None:
                error.rejected_output = rejected_output
            if cause is not None:
                raise error from cause
            raise error
        probe_instruction = ""
        if capability_probe:
            probe_instruction = (
                "\n这是启动能力探测：请返回空 questions 数组。"
                if stage == "clarification"
                else "\n这是启动能力探测：必须先调用 read_skill_file 完整读取 SKILL.md，收到工具结果后再提交符合 Schema 的 JSON。"
            )
        stage_goal = STAGE_PROMPTS[stage]
        if system_instruction:
            stage_goal = f"{stage_goal}\n当前契约补充要求：{system_instruction.strip()}"
        conversation: list[Any] = [{"role": "system", "content": f"当前阶段：{stage}\n阶段目标：{stage_goal}\n{override}\n{tool_contract}{probe_instruction}\n{_output_contract(local_schema)}"}, {"role": "user", "content": input_json}]
        effective_round_budget = min(self.max_steps, self.max_provider_calls)
        convergence_step = max(1, (effective_round_budget * 4 + 4) // 5)
        for step in range(1, self.max_steps + 1):
            active_step = step
            checkpoint()
            if self.clock() - started >= self.timeout_seconds:
                fail("Agent 运行超时，未提交阶段产物", "deadline_exceeded")
            checkpoint()
            try:
                tool_choice = None
                entry_read = SKILL_ENTRY in successful_read_paths
                remaining_text_files = frozenset(available_text_files - completed_read_paths)
                request_tools = _tools_for_stage(
                    stage,
                    self.skill,
                    entry_read=entry_read,
                    remaining_text_files=remaining_text_files,
                )
                request_allowed_files = allowed_skill_files
                missing_entry = stage in SKILL_STAGES and not entry_read
                if missing_entry and not capability_probe:
                    request_tools = [_read_skill_file_tool(frozenset({SKILL_ENTRY}), paginated=False)]
                    tool_choice = {"type": "function", "name": "read_skill_file"}
                elif (
                    force_final_output
                    or provider_call_budget.claimed >= self.max_provider_calls - self.reserved_final_calls
                    or (stage in SKILL_STAGES and not recovery_active and skill_bytes >= self.max_skill_bytes)
                ):
                    request_tools = []
                    request_allowed_files = frozenset()
                    tool_choice = "none"
                    force_final_output = True
                if recovery_active and not force_final_output and not missing_entry:
                    request_tools = self._recovery_tools(
                        stage,
                        self.skill,
                        remaining_paths,
                    )
                    if not request_tools:
                        tool_choice = "none"
                if not request_tools:
                    tool_choice = "none"
                if capability_probe and stage != "clarification":
                    tool_choice = {"type": "function", "name": "read_skill_file"} if tool_count == 0 else "none"
                request_schema = None if (
                    missing_entry and not capability_probe
                    or capability_probe and stage != "clarification" and tool_count == 0
                ) else provider_schema
                if step == convergence_step:
                    conversation.append({
                        "role": "user",
                        "content": "当前阶段已接近模型请求预算的 80%。请停止非必要探索，优先利用已有信息提交符合 Schema 的完整最终结果。",
                    })
                    audit.append({"step": step, "event": "budget_convergence", "threshold": 80})
                progress("waiting_model", f"等待模型响应（第 {step} 轮）", metrics())
                request_input_bytes = len(json.dumps(conversation, ensure_ascii=False, separators=(",", ":")).encode())
                request_conversation_items = len(conversation)
                kwargs = {
                    "input": conversation,
                    "tools": request_tools,
                    "response_schema": request_schema,
                    "tool_choice": tool_choice,
                }
                # Clients that support a request timeout receive the absolute
                # stage remainder. Legacy/test clients keep their old signature.
                try:
                    if getattr(self.client, "supports_execution_cancellation", False):
                        run_remaining = max(0.0, self.timeout_seconds - (self.clock() - started))
                        remaining = min(run_remaining, remaining_seconds(run_remaining))
                        turn = self.client.create(
                            **kwargs,
                            timeout_seconds=remaining,
                            provider_call_budget=provider_call_budget,
                        )
                    else:
                        turn = interruptible(lambda: self.client.create(**kwargs))
                except TypeError as exc:
                    if "timeout_seconds" not in str(exc) and "provider_call_budget" not in str(exc): raise
                    turn = interruptible(lambda: self.client.create(**kwargs))
            except (IndexError, StopIteration) as exc:
                fail("Agent 未在工具纠错后提交阶段产物", "incomplete_after_tool_error", exc)
            except GatewayError as exc:
                phase = probe_phase(exc.code) if capability_probe else None
                if capability_probe and stage != "clarification" and tool_count > 0:
                    audit.append({
                        "event": "terminal",
                        "reason": "provider_error_after_tool_result",
                        "probe_phase": "tool_result",
                        "tool_calls": tool_count,
                        "underlying_code": exc.code,
                        **exc.safe_audit_details(),
                    })
                    self.last_audit = tuple(audit)
                    error = GatewayError(
                        "模型端点未接受工具结果回传，请检查工具续轮兼容性",
                        code="probe_tool_round_failed",
                        status=exc.status,
                        retryable=exc.retryable,
                        retry_after_seconds=exc.retry_after_seconds,
                        audit_details=exc.safe_audit_details(),
                        probe_phase="tool_result",
                        terminal_reason="provider_error_after_tool_result",
                        tool_calls=tool_count,
                        underlying_code=exc.code,
                    )
                    error.audit = self.last_audit
                    raise error from exc
                audit.append({
                    "event": "terminal",
                    "reason": exc.code,
                    "tool_calls": tool_count,
                    **({"probe_phase": phase} if phase else {}),
                    **exc.safe_audit_details(),
                })
                self.last_audit = tuple(audit)
                exc.audit = self.last_audit
                if capability_probe:
                    exc.probe_phase = phase
                    exc.terminal_reason = exc.code
                    exc.tool_calls = tool_count
                raise
            checkpoint()
            progress("provider_response", f"模型响应已返回（第 {step} 轮）", metrics())
            if self.clock() - started >= self.timeout_seconds:
                fail("Agent 运行超时，未提交阶段产物", "deadline_exceeded")
            output = (turn.text or "").encode()
            if len(output) > self.max_output_bytes:
                fail("Agent 最终输出超过大小上限", "output_limit")
            audit.append({"step": step, "event": "model", "response_id_sha256": hashlib.sha256((turn.response_id or "").encode()).hexdigest(), "output_sha256": hashlib.sha256(output).hexdigest(), "input_bytes": request_input_bytes, "conversation_items": request_conversation_items, "cumulative_skill_bytes": skill_bytes, "unique_skill_files": len(successful_read_paths), "repeated_skill_reads": repeated_read_count})
            if turn.tool_calls:
                if not stage_tools:
                    fail("澄清阶段不允许工具调用", "unauthorized_tool")
                if capability_probe and tool_count == 0 and (
                    len(turn.tool_calls) != 1
                    or turn.tool_calls[0].name != "read_skill_file"
                ):
                    fail("模型未按要求完成确定性工具调用", "capability_probe_failed")
                exploration_rounds += 1
                successful_calls, failed_calls = 0, 0
                request_tool_names = {tool["name"] for tool in request_tools}
                for call in turn.tool_calls:
                    checkpoint()
                    error = None
                    try:
                        args = json.loads(call.arguments or "{}")
                        if not isinstance(args, dict): raise ValidationError("工具参数必须为 object")
                    except (json.JSONDecodeError, ValidationError) as exc:
                        args, error = {}, {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
                    requested_path = args.get("path") if isinstance(args, dict) else None
                    normalized_path = self.skill.normalize_tool_path(requested_path) if isinstance(requested_path, str) else None
                    progress("skill_loading", f"调用 Skill 工具：{call.name}", {
                        **metrics(),
                        "tool_calls": tool_count + 1,
                        "tool_name": call.name,
                    })
                    tool_count += 1
                    if tool_count > self.max_tool_calls:
                        fail("Agent 工具调用超过上限", "tool_call_limit")
                    offset = args.get("offset", 0) if isinstance(args, dict) else 0
                    limit = args.get("limit") if isinstance(args, dict) else None
                    read_key = (normalized_path, offset, limit)
                    repeated = call.name == "read_skill_file" and read_key in successful_read_keys
                    if error is None and repeated:
                        repeated_read_count += 1
                        try:
                            result = self.skill.dispatch(call.name, args, allowed_files=request_allowed_files)
                            result = {**result, "already_read": True, "cached": True}
                            force_final_output = True
                        except (ValidationError, OSError, ValueError) as exc:
                            result = {"ok": False, "error": {"code": self._tool_error_code(call.name, str(exc)), "message": str(exc)}}
                    elif error is None and call.name not in request_tool_names:
                        result = {"ok": False, "error": {"code": "unauthorized_tool", "message": "当前轮次未授权该工具；请使用已提供的工具或直接提交最终 JSON"}}
                    else:
                        try:
                            result = error or self.skill.dispatch(call.name, args, allowed_files=request_allowed_files)
                        except (ValidationError, OSError, ValueError) as exc:
                            result = {"ok": False, "error": {"code": self._tool_error_code(call.name, str(exc)), "message": str(exc)}}
                    failed = result.get("ok") is False
                    if failed:
                        failed_calls += 1
                    else:
                        successful_calls += 1
                        if call.name == "read_skill_file" and not repeated:
                            successful_read_keys.add(read_key)
                            if isinstance(result.get("path"), str):
                                if result["path"] not in successful_read_paths:
                                    successful_read_count += 1
                                successful_read_paths.add(result["path"])
                                if result.get("eof") is True:
                                    completed_read_paths.add(result["path"])
                                if isinstance(result.get("sha256"), str):
                                    successful_read_digests[result["path"]] = result["sha256"]
                            if isinstance(result.get("bytes"), int):
                                skill_bytes += result["bytes"]
                    audit.append({"step": step, "event": "tool_error" if failed else "tool", "tool": call.name, "error_code": result.get("error", {}).get("code") if failed else None, "advisory_code": result.get("advisory", {}).get("code") if isinstance(result.get("advisory"), dict) else None, "script_succeeded": result.get("script_succeeded") if call.name == "run_skill_script" else None, "call_id_sha256": hashlib.sha256((call.call_id or "").encode()).hexdigest(), "requested_path_sha256": hashlib.sha256(requested_path.encode()).hexdigest() if isinstance(requested_path, str) and requested_path else None, "path": result.get("path"), "file_sha256": result.get("sha256"), "result_bytes": result.get("bytes", result.get("output_bytes", 0)), "repeated": repeated, "cumulative_skill_bytes": skill_bytes, "unique_skill_files": len(successful_read_paths), "result_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
                    completed_path = result.get("path")
                    completed_label = f"{call.name} · {completed_path}" if completed_path else call.name
                    progress(
                        "skill_completed",
                        f"Skill 工具{'失败' if failed else '完成'}：{completed_label}",
                        {**metrics(), "tool_name": call.name, "tool_path": completed_path, "tool_failed": failed},
                    )
                    conversation.append({"type": "function_call", "name": call.name, "arguments": call.arguments, "call_id": call.call_id})
                    conversation.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, ensure_ascii=False)})
                if failed_calls and not recovery_active:
                    tool_error_corrections = 1
                    recovery_active = True
                if recovery_active:
                    remaining_paths = self._remaining_paths(
                        available_text_files,
                        completed_read_paths,
                    )
                # Budget failed *model rounds*, not individual calls.  Every
                # call in one response is processed and fed back as one batch.
                tool_error_rounds = tool_error_rounds + 1 if successful_calls == 0 else 0
                if successful_calls == 0:
                    audit.append({"step": step, "event": "tool_error_round", "attempt": tool_error_rounds, "calls": len(turn.tool_calls)})
                    if tool_error_rounds >= self.max_tool_error_rounds:
                        fail("阶段工具契约连续不满足，生成已停止", "tool_error_limit")
                if recovery_active:
                    instruction = self._tool_recovery_instruction(remaining_paths)
                    conversation.append({"role": "user", "content": instruction})
                    audit.append({"step": step, "event": "tool_recovery_instruction", "attempt": tool_error_corrections})
                if stage in SKILL_STAGES and SKILL_ENTRY not in successful_read_paths:
                    if entry_protocol_corrections >= 1:
                        fail("模型在一次协议纠正后仍未读取 SKILL.md", "skill_entry_not_read")
                    entry_protocol_corrections += 1
                    conversation.append({"role": "user", "content": "Skill 工具协议纠正（仅此一次）：提交最终结果或读取其他资源前，必须先调用 read_skill_file 完整读取 SKILL.md。"})
                    audit.append({"step": step, "event": "skill_entry_protocol_correction", "attempt": entry_protocol_corrections})
                continue
            if capability_probe and stage != "clarification" and tool_count == 0:
                fail("模型忽略了强制工具调用要求", "capability_probe_failed")
            if stage in SKILL_STAGES and SKILL_ENTRY not in successful_read_paths:
                if entry_protocol_corrections >= 1:
                    fail("模型在一次协议纠正后仍未读取 SKILL.md", "skill_entry_not_read")
                entry_protocol_corrections += 1
                conversation.extend([
                    {"role": "assistant", "content": turn.text or ""},
                    {"role": "user", "content": "Skill 工具协议纠正（仅此一次）：必须先调用 read_skill_file 完整读取 SKILL.md，再根据文档指引按需读取资源或提交最终 JSON。"},
                ])
                audit.append({"step": step, "event": "skill_entry_protocol_correction", "attempt": entry_protocol_corrections})
                continue
            progress("validating_output", "校验模型输出与阶段 Schema", metrics())
            try:
                value = _extract_json_object(turn.text or "")
            except json.JSONDecodeError as exc:
                if schema_corrections < self.max_schema_corrections:
                    schema_corrections += 1
                    audit.append({"step": step, "event": "schema_correction", "reason": "invalid_json", "attempt": schema_corrections})
                    conversation.extend([{"role": "assistant", "content": turn.text or ""}, {"role": "user", "content": "上次输出不是有效 JSON。请仅按已提供的 JSON Schema 重新输出完整 JSON；不要调用工具，不要添加解释。"}])
                    force_final_output = True
                    continue
                fail("Agent 最终输出不是有效 JSON", "invalid_output", exc)
            if not isinstance(value, dict):
                fail("Agent 最终输出必须为 JSON object", "invalid_output")
            if stage in RENDERING_STAGES and payload.get("slide_ids"):
                try:
                    # Normalize before Schema validation so older providers that
                    # omit the newly explicit design fields remain compatible.
                    # Deck responses inherit the exact confirmed sample design;
                    # sample responses receive deterministic defaults.
                    if "design_intent" not in value and payload.get("confirmed_design_intent") is not None:
                        value["design_intent"] = payload["confirmed_design_intent"]
                    if "shared_assets" not in value and payload.get("confirmed_shared_assets") is not None:
                        value["shared_assets"] = payload["confirmed_shared_assets"]
                    value = normalize_rendering_output(value, list(payload.get("slide_ids") or []))
                except GatewayError as exc:
                    if schema_corrections < self.max_schema_corrections:
                        schema_corrections += 1
                        audit.append({"step": step, "event": "technical_correction", "reason": "html_fragment_contract", "attempt": schema_corrections})
                        conversation.extend([
                            {"role": "assistant", "content": turn.text or ""},
                            {"role": "user", "content": (
                                f"technical_correction：{exc.message}。请重新提交完整 slides、design_intent 与 shared_assets；"
                                "每项 html 必须且只能是对应页面的单个 <section class=\"slide\" "
                                "id=\"给定ID\" data-slide-id=\"给定ID\">...</section> 片段，"
                                "不得包含 html/body 外壳、额外页面或解释文字。"
                            )},
                        ])
                        force_final_output = True
                        continue
                    fail(exc.message, "invalid_output", exc)
            try:
                self._validate_schema(value, local_schema.get("schema", local_schema), "output")
            except GatewayError as exc:
                if schema_corrections < self.max_schema_corrections:
                    schema_corrections += 1
                    audit.append({"step": step, "event": "schema_correction", "reason": "schema_validation", "attempt": schema_corrections})
                    conversation.extend([{"role": "assistant", "content": turn.text or ""}, {"role": "user", "content": f"上次输出未通过 Schema 校验：{exc.message}。请仅按已提供的 JSON Schema 重新输出完整 JSON；不要调用工具，不要添加解释。"}])
                    force_final_output = True
                    continue
                fail(exc.message, "invalid_output", exc)
            if result_validator is not None:
                progress("validating_evidence", "校验事实证据与业务约束", metrics())
                try:
                    semantic_evidence = result_validator(value)
                except Exception as exc:
                    if semantic_corrections < max_semantic_corrections:
                        semantic_corrections += 1
                        audit.append({
                            "step": step,
                            "event": "semantic_correction",
                            "attempt": semantic_corrections,
                            "error_type": type(exc).__name__,
                        })
                        progress("correcting", "业务校验未通过，正在进行一次语义纠错", metrics())
                        conversation.extend([
                            {"role": "assistant", "content": turn.text or ""},
                            {"role": "user", "content": f"上次候选未通过证据/业务校验：{str(exc)}。请保留已读取的 Skill 与证据上下文，修正违规字段后重新提交完整 JSON；不要调用工具，不要添加解释。"},
                        ])
                        force_final_output = True
                        continue
                    fail("Agent 候选在一次语义纠错后仍未通过业务校验", "invalid_output", exc, value)
            if capability_probe and stage != "clarification" and not any(
                item.get("event") == "tool"
                and item.get("tool") == "read_skill_file"
                and item.get("path") == SKILL_ENTRY
                for item in audit
            ):
                fail("模型未完成工具能力探测", "capability_probe_failed")
            audit.append({"event": "terminal", "reason": "success", "tool_calls": tool_count, "provider_calls": provider_call_budget.claimed, "exploration_rounds": exploration_rounds, "cumulative_skill_bytes": skill_bytes, "unique_skill_files": len(successful_read_paths), "applied_skill_files": sorted(successful_read_paths), "skill_entry_read": SKILL_ENTRY in successful_read_paths, "repeated_skill_reads": repeated_read_count})
            progress("agent_completed", "模型输出已返回，正在执行技术校验", metrics())
            self.last_audit = tuple(audit)
            return AgentResult(value, self.last_audit, turn.response_id, semantic_evidence)
        fail("Agent 达到最大步数，未提交阶段产物", "max_steps")

    @staticmethod
    def _failure_code(reason: str, capability_probe: bool, tool_calls: int = 0) -> str:
        if not capability_probe:
            if reason == "tool_error_limit":
                return "stage_tool_contract_error"
            return {
                "deadline_exceeded": "agent_run_deadline_exceeded",
                "max_steps": "agent_step_limit",
                "tool_call_limit": "agent_tool_call_limit",
                "output_limit": "agent_output_limit",
                "invalid_output": "agent_invalid_output",
                "incomplete_after_tool_error": "agent_incomplete_after_tool_error",
                "unauthorized_tool": "agent_unauthorized_tool",
                "skill_entry_not_read": "agent_skill_entry_missing",
            }.get(reason, "gateway_error")
        if reason == "invalid_output":
            return "probe_tool_final_invalid_output" if tool_calls > 0 else "probe_invalid_output"
        if reason == "capability_probe_failed":
            return "probe_tool_call_missing"
        if reason == "max_steps":
            return "probe_step_limit"
        if reason == "deadline_exceeded":
            return "probe_deadline_exceeded"
        if reason in {"unauthorized_tool", "tool_call_limit", "tool_error_limit", "incomplete_after_tool_error"}:
            return "probe_tool_round_failed"
        if reason == "output_limit":
            return "probe_output_limit"
        return "capability_probe_failed"

    @staticmethod
    def _stage_tool_contract(
        stage: str,
        skill: SkillRuntime,
    ) -> str:
        if stage == "clarification":
            return "工具契约：本阶段没有可用工具，请直接提交最终 JSON。"
        return (
            f"Skill 发现：名称={json.dumps(skill.skill_name, ensure_ascii=False)}；"
            f"描述={json.dumps(skill.skill_description, ensure_ascii=False)}。"
            "工具协议：必须首先调用 read_skill_file 完整读取 SKILL.md；"
            "随后依据 SKILL.md 中的相对路径，按需使用 list_skill_files、read_skill_file、"
            "get_asset_info 或可选的 run_skill_script。不要一次性读取全部资源或重复读取。"
            "run_skill_script 的成功、失败与超时都只是自检建议，不决定任务或交付是否成功。"
            "若没有读取入口文件，协议只纠正一次；达到探索、文件、字节或模型请求预算后必须提交最终 JSON。"
        )

    @staticmethod
    def _recovery_tools(
        stage: str,
        skill: SkillRuntime,
        remaining_paths: frozenset[str] | None,
    ) -> list[dict]:
        del stage, skill
        return [_read_skill_file_tool(remaining_paths)] if remaining_paths else []

    @staticmethod
    def _remaining_paths(
        available_text_files: frozenset[str],
        completed_read_paths: set[str],
    ) -> frozenset[str] | None:
        return frozenset(available_text_files - completed_read_paths)

    @staticmethod
    def _tool_recovery_instruction(remaining_paths: frozenset[str] | None) -> str:
        if remaining_paths:
            allowed = "、".join(sorted(remaining_paths))
            return (
                "当前处于受限恢复轮。如仍需读取，只能调用 read_skill_file，"
                f"且 path 必须从以下合法路径选择：{allowed}；否则请不要调用工具，直接提交符合 Schema 的最终 JSON。"
            )
        return "当前受限恢复轮已无剩余合法读取路径。下一轮不得调用工具，请直接提交符合 Schema 的最终 JSON。"

    @staticmethod
    def _tool_error_code(name: str, message: str) -> str:
        if name not in {item["name"] for item in TOOLS}:
            return "unauthorized_tool"
        if "上限" in message:
            return "quota_exceeded"
        if any(marker in message for marker in ("路径", "白名单", "固定文件", "快照", "Asset")):
            return "path_not_in_lock"
        return "tool_validation_error"

    @classmethod
    def _text_only(cls, value):
        """Strip binary image payloads while retaining stable resource references."""
        if isinstance(value,dict): return {k:cls._text_only(v) for k,v in value.items()}
        if isinstance(value,list): return [cls._text_only(v) for v in value]
        if isinstance(value,tuple): return [cls._text_only(v) for v in value]
        if isinstance(value,str) and value.lstrip().lower().startswith(DATA_IMAGE): return "[image-content-removed]"
        return value

    def _validate_schema(self, value: Any, schema: dict, path: str) -> None:
        """Small strict subset used as a defensive check after provider validation."""
        expected = schema.get("type")
        valid = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }.get(expected, True)
        if not valid:
            raise GatewayError(f"Agent 输出不符合 Schema：{path} 类型无效")
        if "enum" in schema and value not in schema["enum"]:
            raise GatewayError(f"Agent 输出不符合 Schema：{path} 枚举无效")
        if expected == "string" and len(value) < schema.get("minLength", 0):
            raise GatewayError(f"Agent 输出不符合 Schema：{path} 长度不足")
        if expected == "string" and "pattern" in schema and not re.search(schema["pattern"], value):
            raise GatewayError(f"Agent 输出不符合 Schema：{path} 内容为空")
        if expected == "object":
            properties, required = schema.get("properties", {}), schema.get("required", [])
            missing = [name for name in required if name not in value]
            if missing:
                raise GatewayError(f"Agent 输出不符合 Schema：{path} 缺少字段 {','.join(missing)}")
            if schema.get("additionalProperties") is False and set(value) - set(properties):
                raise GatewayError(f"Agent 输出不符合 Schema：{path} 包含未知字段")
            for name, item in value.items():
                if name in properties:
                    self._validate_schema(item, properties[name], f"{path}.{name}")
        elif expected == "array":
            if len(value) < schema.get("minItems", 0):
                raise GatewayError(f"Agent 输出不符合 Schema：{path} 元素不足")
            if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value}) != len(value):
                raise GatewayError(f"Agent 输出不符合 Schema：{path} 元素重复")
            for index, item in enumerate(value):
                self._validate_schema(item, schema.get("items", {}), f"{path}[{index}]")
