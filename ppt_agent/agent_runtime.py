from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from .errors import GatewayError, ValidationError
from .skill_runtime import SkillRuntime


STAGES = {"clarification", "narrative", "outline", "sample", "deck", "inspection"}
DATA_IMAGE = "data:image/"
STAGE_PROMPTS = {
    "clarification": (
        "直接依据原始任务卡、规范化结果和资源摘要，仅提出真正阻碍交付的 0 到 5 个上下文相关问题；"
        "不得重复询问已知事实。每个问题必须包含稳定 question_id、目标 field_path、明确 prompt、"
        "helper_text、0 个或多个带 value/label/description 的 options、allow_other 与 blocking。"
        "本阶段不提供也不需要任何 Skill 工具，禁止请求工具。"
    ),
    "narrative": "根据任务卡生成叙事结构 Markdown；不要生成逐页 HTML。",
    "outline": "根据已确认叙事生成逐页大纲 Markdown；保持页面标识稳定。",
    "sample": "仅为外层状态机指定的样品页生成完整 HTML，不得扩展到全稿。",
    "deck": "为外层状态机给定的全部页面生成完整 HTML，并遵守已确认样品的视觉基线。",
    "inspection": "独立检查大纲与 HTML，仅报告有证据的问题，不得直接修改产物。",
}


def _object_schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


STAGE_OUTPUT_SCHEMAS = {
    "clarification": {"name": "clarification", "strict": True, "schema": _object_schema({"questions": {"type": "array", "items": _object_schema({
        "question_id": {"type": "string"}, "field_path": {"type": "string"}, "prompt": {"type": "string"},
        "helper_text": {"type": "string"}, "options": {"type": "array", "items": _object_schema({
            "value": {"type": "string"}, "label": {"type": "string"}, "description": {"type": "string"}
        }, ["value", "label", "description"])}, "allow_other": {"type": "boolean"}, "blocking": {"type": "boolean"}
    }, ["question_id", "field_path", "prompt", "helper_text", "options", "allow_other", "blocking"])}}, ["questions"])},
    "narrative": {"name": "narrative", "strict": True, "schema": _object_schema({"markdown": {"type": "string"}}, ["markdown"])},
    "outline": {"name": "outline", "strict": True, "schema": _object_schema({"markdown": {"type": "string"}}, ["markdown"])},
    "sample": {"name": "sample_html", "strict": True, "schema": _object_schema({"html": {"type": "string"}}, ["html"])},
    "deck": {"name": "deck_html", "strict": True, "schema": _object_schema({"html": {"type": "string"}}, ["html"])},
    "inspection": {"name": "inspection", "strict": True, "schema": _object_schema({"passed": {"type": "boolean"}, "issues": {"type": "array", "items": _object_schema({
        "issue_id": {"type": "string"}, "severity": {"type": "string", "enum": ["warning", "blocker"]},
        "level": {"type": "string", "enum": ["element", "slide", "deck"]}, "code": {"type": "string"},
        "message": {"type": "string"}, "slide_id": {"type": "string"}, "element_id": {"type": "string"},
        "evidence": {"type": "string"}, "suggestion": {"type": "string"},
    }, ["issue_id", "severity", "level", "code", "message", "slide_id", "element_id", "evidence", "suggestion"])}}, ["passed", "issues"])},
}
PRODUCT_OVERRIDE = """产品规则高于 Skill：你只处理当前阶段，不得推进工作流或请求状态机操作。
仅允许纯文本输入；禁止联网、图片输入、图片生成、Shell、文件写入、自更新和安装依赖。
按需使用只读 Skill 工具；不要把整个 Skill 一次性读入。最终仅返回符合指定 JSON Schema 的 JSON。"""
CLARIFICATION_OVERRIDE = """产品规则高于 Skill：你只处理澄清阶段，不得推进工作流或请求状态机操作。
仅允许纯文本输入；禁止联网、图片输入、图片生成、Shell、文件读写、自更新和安装依赖。
当前请求没有可用工具；直接依据输入作答。最终仅返回符合指定 JSON Schema 的 JSON。"""

TOOLS = [
    {"type": "function", "name": "list_skill_files", "description": "列出可读取的标准 Skill 文件", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "read_skill_file", "description": "读取一个白名单内 UTF-8 Skill 文本文件", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
    {"type": "function", "name": "get_asset_info", "description": "读取一个 Skill asset 的元数据，不读取二进制内容", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
]


@dataclass(frozen=True)
class AgentResult:
    value: dict
    audit: tuple[dict, ...]
    response_id: str | None


class AgentRuntime:
    def __init__(self, client, skill: SkillRuntime, *, max_steps: int = 12, timeout_seconds: float = 60, clock=time.monotonic, max_output_bytes: int = 1024 * 1024, max_tool_calls: int = 24, max_schema_corrections: int = 1, max_tool_error_rounds: int = 2):
        self.client, self.skill = client, skill
        self.max_steps, self.timeout_seconds, self.clock = max_steps, timeout_seconds, clock
        self.max_output_bytes, self.max_tool_calls = max_output_bytes, max_tool_calls
        if isinstance(max_schema_corrections, bool) or not isinstance(max_schema_corrections, int) or not 0 <= max_schema_corrections <= 2:
            raise ValidationError("Schema 纠错次数必须是 0 到 2 的整数")
        if isinstance(max_tool_error_rounds, bool) or not isinstance(max_tool_error_rounds, int) or not 1 <= max_tool_error_rounds <= 3:
            raise ValidationError("工具错误轮次必须是 1 到 3 的整数")
        self.max_schema_corrections = max_schema_corrections
        self.max_tool_error_rounds = max_tool_error_rounds
        self.last_audit: tuple[dict, ...] = ()

    def run(self, stage: str, payload: dict, *, response_schema: dict | None = None, capability_probe: bool = False) -> AgentResult:
        if stage not in STAGES:
            raise ValidationError("Agent 阶段不在允许列表")
        if response_schema is not None and response_schema != STAGE_OUTPUT_SCHEMAS[stage]:
            raise ValidationError("阶段输出 Schema 不允许覆盖")
        response_schema = STAGE_OUTPUT_SCHEMAS[stage]
        if not isinstance(payload, dict):
            raise ValidationError("Agent 输入无效")
        payload = self._text_only(payload)
        stage_tools = [] if stage == "clarification" else TOOLS
        override = CLARIFICATION_OVERRIDE if stage == "clarification" else PRODUCT_OVERRIDE
        started, audit, tool_count, tool_error_rounds, schema_corrections = self.clock(), [], 0, 0, 0
        input_json=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        audit.append({"event": "run", "stage": stage, "skill": self.skill.skill_name, "skill_version": self.skill.skill_version, "lock_sha256": hashlib.sha256(json.dumps(self.skill.manifest, sort_keys=True).encode()).hexdigest(), "input_sha256": hashlib.sha256(input_json.encode()).hexdigest(), "config_sha256": hashlib.sha256((STAGE_PROMPTS[stage]+override).encode()).hexdigest()})
        def fail(message: str, reason: str, cause=None):
            audit.append({"event": "terminal", "reason": reason, "tool_calls": tool_count})
            self.last_audit = tuple(audit)
            error = GatewayError(message, code=self._failure_code(reason, capability_probe))
            error.audit = self.last_audit
            if cause is not None:
                raise error from cause
            raise error
        probe_instruction = ""
        if capability_probe:
            probe_instruction = (
                "\n这是启动能力探测：请返回空 questions 数组。"
                if stage == "clarification"
                else "\n这是启动能力探测：必须先调用一次 list_skill_files，收到工具结果后再提交符合 Schema 的 JSON。"
            )
        conversation: list[Any] = [{"role": "system", "content": f"当前阶段：{stage}\n阶段目标：{STAGE_PROMPTS[stage]}\n{override}{probe_instruction}"}, {"role": "user", "content": input_json}]
        for step in range(1, self.max_steps + 1):
            if self.clock() - started >= self.timeout_seconds:
                fail("Agent 运行超时，未提交阶段产物", "deadline_exceeded")
            try:
                tool_choice = None
                if capability_probe and stage != "clarification":
                    tool_choice = {"type": "function", "name": "list_skill_files"} if tool_count == 0 else "none"
                request_schema = None if capability_probe and stage != "clarification" and tool_count == 0 else response_schema
                turn = self.client.create(input=conversation, tools=stage_tools, response_schema=request_schema, tool_choice=tool_choice)
            except (IndexError, StopIteration) as exc:
                fail("Agent 未在工具纠错后提交阶段产物", "incomplete_after_tool_error", exc)
            except GatewayError as exc:
                audit.append({
                    "event": "terminal",
                    "reason": exc.code,
                    "tool_calls": tool_count,
                    **exc.safe_audit_details(),
                })
                self.last_audit = tuple(audit)
                exc.audit = self.last_audit
                raise
            if self.clock() - started >= self.timeout_seconds:
                fail("Agent 运行超时，未提交阶段产物", "deadline_exceeded")
            output = (turn.text or "").encode()
            if len(output) > self.max_output_bytes:
                fail("Agent 最终输出超过大小上限", "output_limit")
            audit.append({"step": step, "event": "model", "response_id_sha256": hashlib.sha256((turn.response_id or "").encode()).hexdigest(), "output_sha256": hashlib.sha256(output).hexdigest()})
            if turn.tool_calls:
                if not stage_tools:
                    fail("澄清阶段不允许工具调用", "unauthorized_tool")
                if capability_probe and tool_count == 0 and (len(turn.tool_calls) != 1 or turn.tool_calls[0].name != "list_skill_files"):
                    fail("模型未按要求完成确定性工具调用", "capability_probe_failed")
                successful_calls = 0
                for call in turn.tool_calls:
                    tool_count += 1
                    if tool_count > self.max_tool_calls:
                        fail("Agent 工具调用超过上限", "tool_call_limit")
                    error = None
                    try:
                        args = json.loads(call.arguments or "{}")
                        if not isinstance(args, dict): raise ValidationError("工具参数必须为 object")
                    except (json.JSONDecodeError, ValidationError) as exc:
                        args, error = {}, {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
                    try:
                        result = error or self.skill.dispatch(call.name, args)
                    except (ValidationError, OSError, ValueError) as exc:
                        result = {"ok": False, "error": {"code": self._tool_error_code(call.name, str(exc)), "message": str(exc)}}
                    failed = result.get("ok") is False
                    if not failed:
                        successful_calls += 1
                    audit.append({"step": step, "event": "tool_error" if failed else "tool", "tool": call.name, "error_code": result.get("error", {}).get("code") if failed else None, "call_id_sha256": hashlib.sha256((call.call_id or "").encode()).hexdigest(), "path": result.get("path"), "file_sha256": result.get("sha256"), "result_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
                    conversation.append({"type": "function_call", "name": call.name, "arguments": call.arguments, "call_id": call.call_id})
                    conversation.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, ensure_ascii=False)})
                # Budget failed *model rounds*, not individual calls.  Every
                # call in one response is processed and fed back as one batch.
                tool_error_rounds = tool_error_rounds + 1 if successful_calls == 0 else 0
                if successful_calls == 0:
                    audit.append({"step": step, "event": "tool_error_round", "attempt": tool_error_rounds, "calls": len(turn.tool_calls)})
                    if tool_error_rounds >= self.max_tool_error_rounds:
                        fail("Agent 工具调用连续失败", "tool_error_limit")
                continue
            try:
                value = json.loads(turn.text or "")
            except json.JSONDecodeError as exc:
                if schema_corrections < self.max_schema_corrections:
                    schema_corrections += 1
                    audit.append({"step": step, "event": "schema_correction", "reason": "invalid_json", "attempt": schema_corrections})
                    conversation.extend([{"role": "assistant", "content": turn.text or ""}, {"role": "user", "content": "上次输出不是有效 JSON。请仅按已提供的 JSON Schema 重新输出完整 JSON；不要调用工具，不要添加解释。"}])
                    continue
                fail("Agent 最终输出不是有效 JSON", "invalid_output", exc)
            if not isinstance(value, dict):
                fail("Agent 最终输出必须为 JSON object", "invalid_output")
            try:
                self._validate_schema(value, response_schema.get("schema", response_schema), "output")
            except GatewayError as exc:
                if schema_corrections < self.max_schema_corrections:
                    schema_corrections += 1
                    audit.append({"step": step, "event": "schema_correction", "reason": "schema_validation", "attempt": schema_corrections})
                    conversation.extend([{"role": "assistant", "content": turn.text or ""}, {"role": "user", "content": f"上次输出未通过 Schema 校验：{exc.message}。请仅按已提供的 JSON Schema 重新输出完整 JSON；不要调用工具，不要添加解释。"}])
                    continue
                fail(exc.message, "invalid_output", exc)
            if capability_probe and stage != "clarification" and not any(item.get("event") == "tool" for item in audit):
                fail("模型未完成工具能力探测", "capability_probe_failed")
            audit.append({"event": "terminal", "reason": "success", "tool_calls": tool_count})
            self.last_audit = tuple(audit)
            return AgentResult(value, self.last_audit, turn.response_id)
        fail("Agent 达到最大步数，未提交阶段产物", "max_steps")

    @staticmethod
    def _failure_code(reason: str, capability_probe: bool) -> str:
        if not capability_probe:
            return "gateway_error"
        if reason == "invalid_output":
            return "probe_invalid_output"
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
    def _tool_error_code(name: str, message: str) -> str:
        if name not in {item["name"] for item in TOOLS}:
            return "unauthorized_tool"
        if "上限" in message:
            return "quota_exceeded"
        if any(marker in message for marker in ("路径", "白名单", "固定文件", "Asset")):
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
            for index, item in enumerate(value):
                self._validate_schema(item, schema.get("items", {}), f"{path}[{index}]")
