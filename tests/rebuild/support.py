from __future__ import annotations

import json
from pathlib import Path

from ppt_agent.generation.contracts import TaskBrief
from ppt_agent.generation.model_gateway import ProviderResponse


THEME = {
    "background": "#F7F8FA",
    "surface": "#FFFFFF",
    "text": "#15171A",
    "muted_text": "#5C6673",
    "primary": "#175CD3",
    "accent": "#F79009",
    "font_heading": "Arial",
    "font_body": "Arial",
    "border_radius": 18,
    "space_unit": 12,
}


def brief(slide_count: int = 6, resources=None) -> TaskBrief:
    resources = resources or []
    return TaskBrief.parse({
        "schema_version": "1.0",
        "goal": "让管理层批准下一阶段投入",
        "audience": "管理团队",
        "topic": "增长计划",
        "slide_count": slide_count,
        "language": "zh-CN",
        "style_preferences": {"density": "concise"},
        "resource_manifest": resources,
        "confirmed_facts": [],
    })


def slide(slide_id: str, role: str, *, layout: str = "metrics", asset_ref: str | None = None) -> dict:
    blocks = [{"type": "paragraph", "block_id": f"body-{slide_id}", "text": f"{slide_id} 的核心信息"}]
    refs = []
    if asset_ref:
        blocks.append({"type": "image", "block_id": f"image-{slide_id}", "asset_ref": asset_ref, "alt": "已确认素材"})
        refs.append(asset_ref)
    return {
        "slide_id": slide_id,
        "role": role,
        "title": f"页面 {slide_id}",
        "content_blocks": blocks,
        "layout_family": layout,
        "asset_refs": refs,
        "speaker_notes": "",
    }


class ContractProvider:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.failure = None

    def create(self, **request):
        self.calls.append(request)
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        name = request["response_schema"]["name"]
        payload = json.loads(request["input"][1]["content"])["input"]
        if name == "narrative_spec_v1":
            output = {
                "schema_version": "1.0",
                "thesis": "聚焦已验证路径可以提高投入效率",
                "audience_takeaway": "按阶段批准资源",
                "story_arc": [
                    {"beat_id": "context", "purpose": "建立背景", "message": "说明当前机会"},
                    {"beat_id": "decision", "purpose": "推动决策", "message": "给出行动路径"},
                ],
                "evidence_refs": [],
                "tone": "克制、清晰",
            }
        elif name == "outline_draft_v1":
            count = payload["slide_count"]
            roles = ["cover", "data", "analysis", "plan", "evidence", "closing"]
            output = {
                "schema_version": "1.0",
                "slides": [
                    {"role": roles[index % len(roles)], "title": f"大纲 {index + 1}", "message": f"消息 {index + 1}", "evidence_refs": [], "visual_intent": "清晰层级"}
                    for index in range(count)
                ],
            }
        elif name == "sample_spec_v1":
            selected = payload["selected_slides"]
            output = {
                "schema_version": "1.0",
                "slides": [slide(item["slide_id"], item["role"], layout="cover" if index == 0 else "metrics") for index, item in enumerate(selected)],
                "theme_tokens": THEME,
                "shared_assets": [],
                "outline_checkpoint_id": payload["outline_checkpoint_id"],
            }
        elif name == "slide_batch_spec_v1":
            output = {
                "schema_version": "1.0",
                "slides": [slide(item["slide_id"], item["role"], layout="metrics") for item in payload["requested_slides"]],
            }
        else:
            raise AssertionError(name)
        response_id = f"resp-{len(self.calls)}"
        response = ProviderResponse(response_id, output)
        self.responses[response_id] = response
        return response

    def retrieve(self, response_id):
        return self.responses[response_id]


class TransportFailure(ConnectionError):
    def __init__(self, *, request_sent, response_id=None, retryable=True):
        super().__init__("transport failed")
        self.request_sent = request_sent
        self.response_id = response_id
        self.retryable = retryable


def asset_record(root: Path, resource_id: str = "image-1") -> dict:
    import hashlib

    content = b"deterministic-image"
    path = root / "source.png"
    path.write_bytes(content)
    return {"resource_id": resource_id, "uri": path.name, "media_type": "image/png", "content_hash": hashlib.sha256(content).hexdigest()}
