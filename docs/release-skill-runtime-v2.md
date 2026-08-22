# SkillRuntime v2 / TechnicalGate v2 发布说明

## 发布范围

本版本完成标准 Skill 运行时、渐进式 Agent 工具协议、通用生成契约、唯一技术门禁与 Job 权威刷新链路的发布收口。旧的阶段 Markdown Loader、旧 HTTP Gateway、服务端固定版式/DOM 校验、canonical/Layout compatibility adapter 已删除；运行时不会长期双持两套实现。

发布前必须同时满足：

- `feature_flags.skill_runtime_v2: true`
- `feature_flags.technical_gate_v2: true`
- 自动化完整矩阵通过，浏览器测试 0 skipped。
- 真实模型旅程从资料导入走到离线交付，并真实触发一次格式纠错。

## 灰度开关语义

```yaml
feature_flags:
  skill_runtime_v2: true
  technical_gate_v2: true
```

这两个开关是实例级写入准入与熔断开关，不是旧实现选择器。关闭任一开关后：

- `/livez` 继续可用，历史任务和工件仍可只读查看。
- `/readyz`、`/healthz` 返回 503，`release.write_enabled=false`。
- 新的 Agent/生成/门禁写链路失败关闭，绝不绕过 SkillRuntime 或 TechnicalGate。
- 状态响应公开两个开关值、`legacy_implementation_present=false` 与版本级回滚模式。

旧实现已经移除，因此同一进程不提供 v1 fallback。灰度期的“旧/新 evidence 双跑”由上一个不可变版本和候选版本两个独立部署承担；两边接收同一脱敏测试输入，只有候选 v2 evidence 用于候选链路判定。不要把旧门禁代码复制回当前进程。

## 自动化矩阵

本地完整验收：

```bash
python3 -m pip install -r requirements-browser.txt
python3 -m playwright install chromium
python3 scripts/verify_release_matrix.py --profile full
```

矩阵包含：

| Lane | 覆盖范围 | 主要证据 |
|---|---|---|
| `architecture` | Skill 替换、框架静态去耦、旧文件/符号缺失、显式依赖注入 | `scripts/verify_release_architecture.py`、`tests/test_generation_decoupling.py` |
| `standard` | 单元、配置与安全、API、E2E、Job/SSE/polling；浏览器套件显式排除并交给独立进程 | `scripts/verify_standard_tests.py`、`tests/**` |
| `contracts` | 产品契约与 AC-01..AC-18 文档完整性 | `scripts/verify_p0.py` |
| `frontend` | 前端 Build 与源码一致 | `scripts/update_frontend_build.py --check` |
| `browser` | 真实 Chromium、同页权威刷新、SSE/polling、渲染/越界/裁切/坏资源 | `scripts/verify_browser_gate.py` |
| `generation` | 样品→全稿→检查→定稿→交付、门禁 evidence 哈希、离线复核 | `scripts/verify_p0_generation_gate.py` |
| `offline` | ZIP/目录完整性、外链与路径安全、离线播放器 | `tests/test_stage_d_offline.py`、`tests/browser/test_ac_16_offline_delivery.py` |

真实模型门禁单独运行，必须在受控环境提供密钥：

```bash
python3 scripts/verify_release_matrix.py --profile real-model
```

该旅程执行能力探测、资料导入、多轮澄清、叙事、大纲、样品、全稿、检查、定稿和离线交付；在叙事阶段故意损坏一次真实模型的已完成 JSON turn，验证同一真实模型能响应 Schema correction 并成功收敛。输出只包含任务 ID、开关、布尔门禁结果和内容哈希，不输出密钥、Prompt 或模型正文。

## 灰度步骤

1. 保留上一个不可变版本，部署候选版本但先把两个开关设为 `false`；确认存活、只读任务与监控正常。
2. 在独立测试实例同时运行上一个版本和候选版本，保存两套脱敏 evidence；候选必须以 v2 判定且完整矩阵全绿。
3. 候选两个开关设为 `true`，按 `5%` 流量观察至少一个发布窗口。
4. 指标无回退后提升到 `25%`，再提升到 `100%`。每档都要重新核对 readiness、Job 失败率、TechnicalGate blocker 分布、SSE→polling 降级率、交付哈希失败和浏览器不可用率。
5. `100%` 稳定后停止旧版本写流量；旧版本仅保留到回滚窗口结束，不回合并旧实现。

推荐发布证据：候选 commit SHA、完整矩阵日志、真实模型 JSON 摘要、配置摘要哈希、各档开始/结束时间和关键指标截图。任何 skipped 都视为失败。

## 回滚

1. 立即把候选实例的两个开关设为 `false`，停止新写入。
2. 将写流量切回上一个不可变版本；不要在候选进程内寻找或重建旧实现。
3. 保留候选已产生的不可变任务版本、TechnicalGate evidence 和交付目录，不手工覆盖或降级。
4. 对结果未知的 Job 先按 Job ID、分支 revision、artifact hash 和 provider request-id 哈希核对，再决定重试。
5. 修复后重新运行完整矩阵和真实模型门禁，从 `5%` 重新开始。

以下任一情况立即回滚：TechnicalGate 技术错误被放行、合法页面被旧 DOM/审美规则阻断、Skill 快照混用、终态同页不能收敛、交付 evidence/hash 不一致、浏览器门禁 skipped，或真实模型格式纠错未收敛。
