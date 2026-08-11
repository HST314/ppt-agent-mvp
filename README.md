# PPT Agent MVP

PPT Agent MVP 的需求、实现决策与验收追踪仓库。

当前处于 P0（基线、勘察与实现决策），尚未进入 P1 运行内核实现。产品行为以 `docs/product-contract.md` 为准，实施顺序以 `docs/development-plan.md` 为准。

## 本地启动

环境要求：Python 3.10+。无需安装第三方依赖。

```bash
python3 scripts/start_p0.py --host 127.0.0.1 --port 8000
```

另一个终端访问 `http://127.0.0.1:8000/healthz`，应得到含 `"stage": "P0"`、`"status": "ok"` 和 `"runtime_ready": false` 的 JSON。该入口是满足 P0 门禁的真实可启动脚手架，不冒充 P1 业务 API、状态机或前端。

## P0 校验

环境要求：Git 与 Python 3.10+。

```bash
python3 scripts/verify_p0.py
```

该命令离线检查 P0 必需文件、18 条验收映射字段、责任任务有效性、P0-01 证据和最小启动入口；不安装依赖、不调用模型。

## 文档入口

- `docs/current-state.md`：仓库及现有能力盘点
- `docs/acceptance-matrix.md`：需求—实现—测试追踪矩阵
- `docs/adr/README.md`：架构决策索引与 MVP 非目标
- `docs/product-contract.md`：已确认产品需求与流程契约
- `docs/development-plan.md`：分阶段开发任务清单

P1 将在这个脚手架之外建立真实业务运行内核、API、前端和测试；P0 不宣称这些能力已经存在。
