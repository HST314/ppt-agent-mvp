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
| AC-14 | 阻断问题未处置不可交付 | P6-04、P7-01 | 问题处置/交付 API；检查页 | `tests/e2e/test_ac_14_delivery_gate.py` |
| AC-15 | 仅明确确认后完成 | P7-01、P7-02 | 交付确认 API；交付操作 | `tests/e2e/test_ac_15_explicit_delivery.py` |
| AC-16 | 交付包内容完整 | P7-02 | 交付 API；结果摘要 | `tests/e2e/test_ac_16_delivery_bundle.py` |
| AC-17 | 交付后派生与非破坏回退 | P7-03、P7-04 | 派生/回退 API；版本页 | `tests/e2e/test_ac_17_post_delivery.py` |
| AC-18 | 桌面端完成全流程 | P8-01～P8-03 | 全工作区 | `tests/browser/test_ac_18_desktop_journey.py` |

## 回填规则

- 文件路径是稳定的预期证据位置；任务实现时创建并将“计划”更新为具体测试名/CI 链接。
- 单元或契约测试不能替代对应 E2E；真实模型不是确定性验收依赖。
- 任一产品行为调整必须先更新产品契约，再同步本矩阵和测试。
