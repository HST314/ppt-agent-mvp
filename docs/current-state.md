# 当前状态与能力盘点

更新日期：2026-08-11  
当前边界：P6 实现与自测完成，等待独立复验；尚未进入 P7。

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

P7 交付与恢复已实现并独立验收通过。P8 第一门禁已实现并待独立复验：真实生成/检查/HTML 与分阶段 Skill 可通过显式配置接入，deterministic fake 继续作为 CI 替身；9 类场景、安全和恢复证据见既有 E2E 与 `tests/test_p8_gateways.py`。第二门禁仍须在固定 Chromium 桌面环境完成不可跳过的完整旅程证据。未取得该浏览器证据前，不得宣称整个 MVP 验收完成。
