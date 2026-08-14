# 澄清 API 联调契约（v1.0.2）

`POST /v1/tasks/{task_id}/input` 的 `source_format` 可取 `auto | json | markdown`，省略时为 `auto`。响应 `task_card.format_detection` 返回 `requested`、`detected`、`confidence`。显式声明与内容不一致时返回 `400 validation_error`，不会静默误解析。

`clarification.details[]` 使用严格字段：

```json
{
  "question_id": "missing-audience",
  "field_path": "audience",
  "field": "audience",
  "prompt": "这份演示主要面向哪类受众？",
  "helper_text": "请选择最符合实际情况的一项，也可以填写自定义答案。",
  "options": [{"value":"公司管理层","label":"公司管理层","description":""}],
  "allow_other": true,
  "blocking": true
}
```

同一对象同时携带 `question_source`（`model | fallback`）、`question_model`、`diagnostic_id`、`question_schema_version`。当前确定性缺口题标记为 `fallback`，前端不得将其展示成“模型生成”。

整轮提交使用 `POST /v1/tasks/{task_id}/clarifications/answers`：

```json
{"answers":{"missing-audience":{"option":"Other","other":"区域经销商"}}}
```

请求必须覆盖本轮所有 `blocking=true` 的问题。服务端先完成全量问题 ID 与答案校验，再进行一次版本写入和一次状态推进；任何一题失败都不产生部分答案。旧单题接口仅保留兼容用途。
