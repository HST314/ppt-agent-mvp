# 当前状态与能力盘点

更新日期：2026-08-11  
范围：P0-01；依据产品契约第 1～3、15 节，不改变产品需求。

## 结论

目标仓库初始为无提交、无默认分支的空仓库，因此没有可继承的 PPT Agent 前端、后端、持久化、模型调用、HTML 预览、配置或测试。P0 已增加依赖无关的最小启动脚手架；P1 才建立业务运行内核。现有 Image Agent 是参考实现，不作为 PPT Agent 的源码依赖。

## 当前目录

```text
ppt-agent-mvp/
├── README.md
├── scripts/
│   ├── start_p0.py
│   └── verify_p0.py
└── docs/
    ├── product-contract.md
    ├── development-plan.md
    ├── current-state.md
    ├── acceptance-matrix.md
    └── adr/
```

## 可执行命令与证据

| 目标 | 命令 | 当前结果 |
|---|---|---|
| 验证远端 | `git ls-remote https://github.com/HST314/ppt-agent-mvp.git` | 无 refs；无默认分支或提交 |
| P0 文档门禁 | `python3 scripts/verify_p0.py` | 应离线通过 |
| 最小启动 | `python3 scripts/start_p0.py --host 127.0.0.1 --port 8000` | `/healthz` 返回 P0 健康信息；不包含业务能力 |
| 启动自检 | `python3 scripts/start_p0.py --check` | 输出“P0 最小启动入口自检通过” |
| 自动测试 | 待 P1 定义 | 当前不存在 |

新员工先阅读产品契约、开发清单和 ADR，再按 README 启动脚手架并运行 P0 校验。P1 完成时必须将 README 更新为真实业务应用的安装、启动和测试命令。

## 已有与缺失

| 能力 | 状态 | 证据/后续任务 |
|---|---|---|
| 产品需求与流程 | 已有 | `docs/product-contract.md` |
| 分阶段任务与依赖 | 已有 | `docs/development-plan.md` |
| 后端/API/状态机 | 缺失 | P1-01～P1-06 |
| 前端工作区 | 缺失 | P2-05 起逐阶段实现 |
| 文件工作区与版本 | 缺失 | P1-03 |
| 模型与 Skill 网关 | 缺失 | P1-05 |
| HTML 构建/预览 | 缺失 | P1-05、P4、P5 |
| 自动化测试 | 缺失 | P1 起按矩阵持续补齐 |
| 配置与凭证加载 | 缺失 | P1-05；凭证只从环境注入 |

## Image Agent 可复用边界及证据

核查基线：`image_agent_mvp` 的 `main`，revision `768471c3efa5aee5032c41468d2438a16d43c8dd`。可用 `git ls-remote https://github.com/HST314/image_agent_mvp.git HEAD refs/heads/main` 复核远端版本。

| 结论 | 源码证据 | PPT 侧处理 |
|---|---|---|
| Pydantic 强契约可复用 | `agent_core/models.py`、`agent_core/contracts.py` | 重新定义 PPT 领域模型 |
| 显式状态与人工门禁可复用 | `agent_core/workflow.py`、`agent_core/gates.py`、`interaction/approval_gate.py` | 重新定义 PPT 阶段与门禁 |
| checkpoint、追加事件、内容 hash 模式可复用 | `storage/project_store.py` | 按 ADR-0002/0005 重新实现并测试 |
| Gateway 与 Presenter 边界可复用 | `model_router/gateway.py`、`interaction/presenter.py` | 保留端口思想，不复制图片载荷 |
| 图片领域流程不可复用 | `workflows/image_mvp_v2_state_machine.yaml`、`calibrator/calibration_loop.py` | 不复制五选一、图片轮次或 VLM/I2I 语义 |
| 图片前端不可直接复用 | `frontend/index.html`、`frontend/static/js/` | PPT 工作区按自身契约实现 |

源码路径可用以下命令复核：

```bash
rg -n "class (StrictBaseModel|ProjectStore|RuntimeModelGateway|Presenter)|waiting_human_approval|checkpoint" agent_core storage model_router interaction calibrator
```

## 风险与技术债

1. P0 最小脚手架只证明仓库能按文档启动，不代表 P1 业务运行内核已完成。
2. HTML 将执行不受信任内容，必须先做隔离预览和资源策略，见 ADR-0004。
3. 文件型持久化仅面向单机 MVP；并行写、原子提交和路径越权需在 P1 测试。
4. 模型调用具有成本与结果未知风险，须持久化调用事实并禁止盲目重试。
5. 产品契约尚未定义具体 HTML 技术 Skill；P4 前必须提供并版本化，但不能阻塞 P1 契约设计。
