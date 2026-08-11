# P1 机器契约

契约版本 `1.0`。领域模型位于 `ppt_agent/schema.py`，导出的 JSON Schema 位于 `schemas/`；新增可选字段保持向后兼容，删除、改名、改变语义或收紧既有字段需要提升主版本并提供迁移器。未知字段一律拒绝。

状态机以 `TaskState(stage, status, mode)` 为唯一事实源。`stage` 表示业务阶段，`status` 表示正交运行状态。样品确认、阻断问题豁免和最终交付必须由 `actor=user` 执行；auto 也不能绕过。生成全稿、检查通过和文件存在均不产生 `completed`。

文件工作区按 task_id 隔离；checkpoint 原子替换，事件追加，版本以 SHA-256 寻址且不可覆盖。命令以 command_id 幂等。公开事件仅记录动作、状态、actor 与时间，不保存 Prompt、模型思维、凭证或供应商原始异常。

Gateway 分为 generation、inspection、skill、HTML builder 四个窄协议。Inspection 仅接受最初大纲和 HTML。P1 提供 deterministic fake，不访问真实模型。

API 见 `docs/openapi.yaml`。错误统一为 `error.code/message/diagnostic_id`；message 是用户可理解文案，diagnostic_id 用于内部关联。
