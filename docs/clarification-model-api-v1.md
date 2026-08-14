# Clarification 模型异步联调契约 v1

agent 模式下，`POST /v1/tasks/{task_id}/input` 先原样冻结输入，再创建 `clarification.generate` Job。响应中的 `clarification.status` 为 `generating`，`details` 必须为空，并包含 `job_id`。前端在 Job 成功前不得展示问题。

`GET /v1/jobs/{job_id}` 返回通用 Job 状态；成功后重新请求 `GET /v1/tasks/{task_id}/input`。澄清对象状态：

- `generating`：`question_source=null`、`details=[]`。
- `ready`：模型成功时 `question_source=model`、`question_model` 非空；完整任务卡允许 `details=[]` 且 `confirmed=true`。
- `failed`：`details=[]`、`question_source=null`，包含 `error.code=clarification_generation_failed`，不会自动生成固定题。

失败重试：`POST /v1/tasks/{task_id}/clarifications/retry`，请求体 `{"idempotency_key":"..."}`，返回 Job。

显式兜底：`POST /v1/tasks/{task_id}/clarifications/fallback`，请求体必须为 `{"confirm":true}`。只有此入口会产生 `question_source=fallback`。

模型输入包含 `original_input`、`original_input_sha256`、`normalized_task_card`、`candidate_missing_fields` 与 `resource_summary`。模型输出严格为 `questions[]`，每题字段为 `question_id / field_path / prompt / helper_text / options[{value,label,description}] / allow_other / blocking`；服务端拒绝未知字段、重复 ID、重复 field_path、重复询问已知 goal/audience/topic、重复选项及超过 5 题的结果，并按 fail-closed 处理。
