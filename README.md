# PPT Agent MVP

PPT Agent MVP 的需求、实现决策与验收追踪仓库。

当前完成 P0（基线、勘察与实现决策），尚未进入运行内核实现。产品行为以 `docs/product-contract.md` 为准，实施顺序以 `docs/development-plan.md` 为准。

## P0 校验

环境要求：Git 与 Python 3.10+。

```bash
python3 scripts/verify_p0.py
```

该命令离线检查 P0 必需文档、18 条验收映射和 ADR 状态；不安装依赖、不调用模型。

## 文档入口

- `docs/current-state.md`：仓库及现有能力盘点
- `docs/acceptance-matrix.md`：需求—实现—测试追踪矩阵
- `docs/adr/README.md`：架构决策索引与 MVP 非目标
- `docs/product-contract.md`：已确认产品需求与流程契约
- `docs/development-plan.md`：分阶段开发任务清单

应用启动、依赖安装及测试命令将在 P1 建立可运行内核时补充；P0 不提供伪造的业务入口。
