# MVP 验收追踪矩阵

范围：P0-02。每个 AC 对应产品契约第 14 节同序号条目。测试文件在对应阶段创建；本表先固定稳定 ID、责任任务、接口/页面和证据位置。

| ID | 验收行为 | 责任任务 | 接口/页面 | 最终证据位置 |
|---|---|---|---|---|
| AC-01 | Markdown/JSON 任务卡创建 | P2-01、P2-05 | 创建任务 API；任务/资料页 | `tests/test_p2.py::test_json_markdown_normalize_and_block`、`test_api_and_desktop_page`、`tests/test_p2_workspace_page.py::test_markdown_card_same_page_closure` |
| AC-02 | 扫描目录并使用图片 Markdown | P2-02、P2-05 | 资源扫描 API；任务/资料页 | `tests/test_p2.py::test_resource_pairing_hash_freeze_and_explicit_rebuild`、`test_corrupt_image_is_diagnosed_and_excluded`、`tests/test_p2_workspace_page.py::test_page_shows_resources_defaults_and_blockers` |
| AC-03 | 阻断等待与可见默认值 | P2-01、P2-04 | 澄清 API；任务/资料页 | `tests/test_p2.py::test_json_markdown_normalize_and_block`、`tests/test_p2_workspace_page.py::test_page_shows_resources_defaults_and_blockers`、`test_empty_state_shows_preconditions` |
| AC-04 | 选择题及 Other 回答 | P2-04、P2-05 | 问答 API；任务/资料页 | `tests/test_p2.py::test_other_answer_and_change_invalidation`、`tests/test_p2_workspace_page.py::test_full_answer_flow_e2e`、`test_other_without_text_rejected` |
| AC-05 | 叙事后逐页大纲且均可编辑 | P3-02、P3-03、P3-05 | 大纲 API；大纲页 | `tests/test_p3.py::test_skill_slices_and_manual_gate`、`test_workspace_has_dual_editors_no_preview` |
| AC-06 | 编辑/Prompt 产生新版本且局部隔离 | P3-04、P5-03 | 修改与版本 API；大纲/全稿页 | `tests/test_p3.py::test_direct_edit_is_authoritative_version`、`test_outline_scope_resource_and_page_validation`、`test_non_destructive_rollback` |
| AC-07 | 默认 2 个可调真实 HTML 样品 | P4-01、P4-02 | 样品生成 API；样品页 | `tests/e2e/test_ac_07_samples.py::AC07SamplesE2E::test_default_two_real_html_samples_reach_sandboxed_page` |
| AC-08 | 样品可反复调整且确认门禁有效 | P4-03、P4-04 | 样品动作 API；样品页 | `tests/e2e/test_ac_08_sample_gate.py::AC08SampleGateE2E::test_repeated_adjustment_and_confirmation_invalidation`、`tests/test_p4_sample_page.py`（版本时间线/历史预览/差异对比 UI 交互）、`tests/test_p4_sample_page_browser.py`（真实浏览器执行页面 JS：自动识别提交/理解依据/歧义提示/确认门禁） |
| AC-09 | 全稿页数等于最终大纲 | P5-01、P5-02 | 全稿生成 API；全稿页 | `tests/e2e/test_ac_09_full_deck.py` |
| AC-10 | 整稿/页/元素修改、摘要、对比、版本 | P5-03、P5-04 | 修改/版本 API；全稿/版本页 | `tests/e2e/test_ac_10_deck_edit.py` |
| AC-11 | 独立检查模型执行三级检查 | P6-01、P6-02 | 检查 API；检查页 | `tests/e2e/test_ac_11_inspection.py` |
| AC-12 | manual 人审、auto 有界修复复检 | P6-03、P6-04 | 运行模式/检查 API；检查页 | `tests/e2e/test_ac_12_modes.py` |
| AC-13 | 达上限进入人工且不虚假完成 | P6-03、P6-05 | 状态 API；检查页 | `tests/e2e/test_ac_13_round_limit.py` |
| AC-14 | 阻断问题未处置不可交付 | P6-04、P7-01 | 问题处置/交付 API；检查页 | `tests/e2e/test_ac_12_modes.py::AC12ModesE2E.test_disposition_audit_and_delivery_blocker_gate` |
| AC-15 | 仅明确确认后完成 | P7-01、P7-02 | 交付确认 API；交付操作 | `tests/e2e/test_ac_15_17_delivery.py::DeliveryJourney::test_ac15_explicit_confirmation_is_only_completion_path` |
| AC-16 | 交付包内容完整 | P7-02 | 交付 API；结果摘要 | `tests/e2e/test_ac_15_17_delivery.py::DeliveryJourney::test_ac16_bundle_is_complete_runnable_and_hash_verified` |
| AC-17 | 交付后派生与非破坏回退 | P7-03、P7-04 | 派生/回退 API；版本页 | `tests/e2e/test_ac_15_17_delivery.py::DeliveryJourney::test_ac17_delivery_is_immutable_and_new_candidate_requires_reinspection`; `DeliveryFaultTests::test_post_publish_breakpoints_are_idempotently_recoverable` |
| AC-18 | 桌面端完成全流程 | P8-01～P8-03 | 全工作区 | `tests/browser/test_ac_18_desktop_journey.py` |

## P8-06 发布回填（2026-08-12）

| 发布项 | 结论 | 可复验证据 | 遗留限制 |
|---|---|---|---|
| 真实 Gateway 与分阶段 Skill | 通过 | `tests/test_p8_gateways.py`、`tests/e2e/test_audit_regressions.py` | CI 使用确定性 fake；真实模型需显式配置 |
| 安全、资源授权与大小边界 | 通过 | `tests/test_p8_release.py`、`tests/test_p4.py` | 单请求 2 MiB；单资源 16 MiB |
| 动作级可观测性 | 通过 | `tests/test_p8_release.py`；JSON `action_metric` | 指标由部署环境采集聚合 |
| 运维与恢复 | 通过 | `docs/runbook.md`、`tests/e2e/test_ac_15_17_delivery.py` | 文件工作区需由部署方纳入备份 |
| AC-01～AC-17 | 通过 | 上表逐项测试路径；非浏览器全量门禁 | 无 |
| AC-18 桌面联合旅程 | 通过 | `tests/browser/test_ac_18_desktop_journey.py`、`scripts/verify_browser_gate.py`；2026-08-12 独立复验：Playwright 1.54.0 / Chromium 139.0.7258.5（build v1181），6/6 passed、0 failed、0 skipped | 固定浏览器环境复验须保持 0 失败、0 跳过，且不得以 API fetch 代替控件操作 |

## 前端 FastAPI 重构第一步（2026-08-13）

| 契约项 | 实现证据 | 自动化证据 |
|---|---|---|
| F1 FastAPI 基础设施 | `ppt_agent/web/app.py`、`ppt_agent/web/routes/tasks.py`、`scripts/start.py` | `tests/web/test_fastapi_app.py`、原有 `tests/e2e/**` 全量回归 |
| F2 设计系统与组件 | `frontend/static/css/**`、`frontend/static/js/components/index.js`、`/components` | `tests/web/test_frontend_assets.py`，四档 Chromium 截图/布局验收 |
| F3 统一应用壳 | `frontend/index.html`、`frontend/static/js/app.js`、`router.js`、`shell.js` | `tests/web/test_fastapi_app.py::FastAPIAppTests::test_health_shell_static_and_legacy_routes` |
| F4 Job/SSE | `ppt_agent/web/jobs.py`、`ppt_agent/web/routes/jobs.py`、`frontend/static/js/job-tracker.js` | `tests/web/test_jobs.py`、`tests/web/test_fastapi_app.py::FastAPIAppTests::test_job_idempotency_sse_and_terminal_reconciliation` |

第一步只迁移首页、任务切换、阶段导航、通用状态和后台任务基础能力。未迁移阶段继续由 `/legacy/tasks/{task_id}/...` 提供兼容交互，并与新壳共享同一 `TaskService`；第二步完成前不删除旧内联页面。

## 回填规则

- 文件路径是稳定的预期证据位置；任务实现时创建并将“计划”更新为具体测试名/CI 链接。
- 单元或契约测试不能替代对应 E2E；真实模型不是确定性验收依赖。
- 任一产品行为调整必须先更新产品契约，再同步本矩阵和测试。
