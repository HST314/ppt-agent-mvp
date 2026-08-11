# 当前状态与能力盘点

更新日期：2026-08-11  
当前边界：P5 实现与自测完成，等待独立复验；尚未进入 P6。

## 已实现

- P0～P1：可启动运行内核、严格 Schema、状态机、原子文件工作区、事件与不可变版本。
- P2：任务卡、澄清、受控资源扫描及冻结输入快照。
- P3：叙事与逐页大纲生成、直接编辑、局部影响域、版本及人工确认。
- P4：样品推荐/改选、真实 HTML、安全沙箱、冻结资源 hash 复核与内嵌、Prompt 作用域识别、精确版本确认门禁。

## P0-01 基线证据（持续保留）

- 审计基线 revision：`768471c3efa5aee5032c41468d2438a16d43c8dd`。
- 核心模型与状态契约：`agent_core/models.py`。
- 原子工作区与项目存储：`storage/project_store.py`。
- 模型路由边界：`model_router/gateway.py`。
- 既有桌面入口：`frontend/index.html`。

这些路径记录的是 P0 对 Image Agent 既有能力的审计证据；后续阶段更新本页时不得删除。

## 运行与验证

```bash
python3 scripts/export_schemas.py
python3 -m unittest discover -s tests -v
python3 scripts/start.py --data .ppt-agent-data
```

健康检查返回 `stage=P4`、`runtime_ready=true`。机器契约见 `docs/openapi.yaml`。

## 尚未开始

P6 独立检查与修复、P7 人工审核交付、P8 完整 E2E/安全验收仍须按开发清单顺序实施。P5 独立复验通过前不得进入 P6。
