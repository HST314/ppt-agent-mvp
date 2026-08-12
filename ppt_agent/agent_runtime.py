from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from .errors import GatewayError, ValidationError
from .skill_runtime import SkillRuntime


STAGES = {"narrative", "outline", "sample", "deck", "inspection"}
STAGE_PROMPTS = {
    "narrative": "根据任务卡生成叙事结构 Markdown；不要生成逐页 HTML。",
    "outline": "根据已确认叙事生成逐页大纲 Markdown；保持页面标识稳定。",
    "sample": "仅为外层状态机指定的样品页生成完整 HTML，不得扩展到全稿。",
    "deck": "为外层状态机给定的全部页面生成完整 HTML，并遵守已确认样品的视觉基线。",
    "inspection": "独立检查大纲与 HTML，仅报告有证据的问题，不得直接修改产物。",
}


def _object_schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


STAGE_OUTPUT_SCHEMAS = {
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
    def __init__(self, client, skill: SkillRuntime, *, max_steps: int = 12, timeout_seconds: float = 60, clock=time.monotonic, max_output_bytes: int = 1024 * 1024, max_tool_calls: int = 24):
        self.client, self.skill = client, skill
        self.max_steps, self.timeout_seconds, self.clock = max_steps, timeout_seconds, clock
        self.max_output_bytes, self.max_tool_calls = max_output_bytes, max_tool_calls
        self.last_audit: tuple[dict, ...] = ()

    def run(self, stage: str, payload: dict, *, response_schema: dict | None = None) -> AgentResult:
        if stage not in STAGES:
            raise ValidationError("Agent 阶段不在允许列表")
        if response_schema is not None and response_schema != STAGE_OUTPUT_SCHEMAS[stage]:
            raise ValidationError("阶段输出 Schema 不允许覆盖")
        response_schema = STAGE_OUTPUT_SCHEMAS[stage]
        if not isinstance(payload, dict):
            raise ValidationError("Agent 输入无效")
        started, audit, tool_count = self.clock(), [], 0
        audit.append({"event": "run", "stage": stage, "skill": self.skill.skill_name, "skill_version": self.skill.skill_version, "lock_sha256": hashlib.sha256(json.dumps(self.skill.manifest, sort_keys=True).encode()).hexdigest()})
        def fail(message: str, reason: str, cause=None):
            audit.append({"event": "terminal", "reason": reason, "tool_calls": tool_count})
            self.last_audit = tuple(audit)
            error = GatewayError(message)
            error.audit = self.last_audit
            if cause is not None:
                raise error from cause
            raise error
        conversation: list[Any] = [{"role": "system", "content": f"当前阶段：{stage}\n阶段目标：{STAGE_PROMPTS[stage]}\n{PRODUCT_OVERRIDE}"}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        for step in range(1, self.max_steps + 1):
            if self.clock() - started >= self.timeout_seconds:
                fail("Agent 运行超时，未提交阶段产物", "deadline_exceeded")
            turn = self.client.create(input=conversation, tools=TOOLS, response_schema=response_schema)
            if self.clock() - started >= self.timeout_seconds:
                fail("Agent 运行超时，未提交阶段产物", "deadline_exceeded")
            output = (turn.text or "").encode()
            if len(output) > self.max_output_bytes:
                fail("Agent 最终输出超过大小上限", "output_limit")
            audit.append({"step": step, "event": "model", "response_id_sha256": hashlib.sha256((turn.response_id or "").encode()).hexdigest(), "output_sha256": hashlib.sha256(output).hexdigest()})
            if turn.tool_calls:
                for call in turn.tool_calls:
                    tool_count += 1
                    if tool_count > self.max_tool_calls:
                        fail("Agent 工具调用超过上限", "tool_call_limit")
                    try:
                        args = json.loads(call.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        fail("Agent 工具参数不是有效 JSON", "invalid_tool_arguments", exc)
                    if not isinstance(args, dict):
                        fail("Agent 工具参数必须为 object", "invalid_tool_arguments")
                    try:
                        result = self.skill.dispatch(call.name, args)
                    except (ValidationError, OSError, ValueError) as exc:
                        fail("Agent 工具调用失败", "tool_error", exc)
                    audit.append({"step": step, "event": "tool", "tool": call.name, "call_id_sha256": hashlib.sha256((call.call_id or "").encode()).hexdigest(), "path": result.get("path"), "file_sha256": result.get("sha256"), "result_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
                    conversation.append({"type": "function_call", "name": call.name, "arguments": call.arguments, "call_id": call.call_id})
                    conversation.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, ensure_ascii=False)})
                continue
            try:
                value = json.loads(turn.text or "")
            except json.JSONDecodeError as exc:
                fail("Agent 最终输出不是有效 JSON", "invalid_output", exc)
            if not isinstance(value, dict):
                fail("Agent 最终输出必须为 JSON object", "invalid_output")
            try:
                self._validate_schema(value, response_schema.get("schema", response_schema), "output")
            except GatewayError as exc:
                fail(exc.message, "invalid_output", exc)
            audit.append({"event": "terminal", "reason": "success", "tool_calls": tool_count})
            self.last_audit = tuple(audit)
            return AgentResult(value, self.last_audit, turn.response_id)
        fail("Agent 达到最大步数，未提交阶段产物", "max_steps")

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
            if not all(name in value for name in required):
                raise GatewayError(f"Agent 输出不符合 Schema：{path} 缺少字段")
            if schema.get("additionalProperties") is False and set(value) - set(properties):
                raise GatewayError(f"Agent 输出不符合 Schema：{path} 包含未知字段")
            for name, item in value.items():
                if name in properties:
                    self._validate_schema(item, properties[name], f"{path}.{name}")
        elif expected == "array":
            for index, item in enumerate(value):
                self._validate_schema(item, schema.get("items", {}), f"{path}[{index}]")
