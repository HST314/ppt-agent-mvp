import { api } from "../api.js?v=2026.08.19.051824935370";
import { badge, button, confirmationDialog, element, field, metadataList, shortHash, versionTimeline } from "../components/index.js?v=2026.08.19.051824935370";
import { actionMessage, parseSlideIds, runAction, section, stageGrid } from "./shared.js?v=2026.08.19.051824935370";
import { comparePanel, deckPreview, modificationPanel } from "./deck.js?v=2026.08.19.051824935370";

export async function render(context) {
  const [view, deckView] = await Promise.all([api.inspection(context.taskId, context.controller), api.deck(context.taskId, context.controller)]);
  context.assertCurrent();
  return reviewStage(view, deckView, context);
}

function reviewStage(view, deckView, context) {
  const report = view.report;
  const preview = view.deck ? deckPreview(view.deck, context.taskId) : null;
  const location = element("p", { className: "muted", role: "status", "aria-live": "polite", text: "选择问题后会在预览中定位对应页面或元素。" });
  const issues = report?.issues || [];
  const active = activeDispositions(view.dispositions || []);
  const blockers = issues.filter((issue) => issue.severity === "blocker");
  const warnings = issues.filter((issue) => issue.severity !== "blocker");
  const historyMessage = actionMessage();
  const mutable = ["deck", "review"].includes(view.state.stage);
  const history = versionTimeline(deckView.versions || [], "deck", {
    currentHash: view.deck?.hash,
    onRollback: mutable ? (item) => runAction({ region: historyMessage, action: () => api.rollbackDeck(context.taskId, item.hash), success: "历史全稿已复制为新的候选版本。", refresh: context.refresh }).catch(() => {}) : null,
  });

  return stageGrid([
    mutable ? finalizePanel(view, context) : frozenPanel(context),
    view.deck ? section("当前候选预览", [location, preview], { description: "可浏览、定位问题；检查不是确定终稿的前置条件。" }) : null,
    mutable ? inspectionControls(view, context) : null,
    mutable && view.deck ? modificationPanel(view.deck, context) : null,
    report?.stale ? section("检查报告已过期", element("div", { className: "notice notice--warning" }, [element("strong", { text: "当前全稿已变化" }), element("p", { text: "旧报告仍可追溯，若需最新质量结论请重新检查；也可以带该状态直接确定终稿。" })])) : null,
    report ? section("阻断问题", mutable ? issueGroup(blockers, active, context, preview, location) : readOnlyIssueGroup(blockers, active, preview, location), { description: mutable ? `${blockers.length} 项；可以修复、记录处置或带遗留问题确定终稿。` : `${blockers.length} 项；终稿冻结后仅供追溯。` }) : null,
    report ? section("普通警告", mutable ? issueGroup(warnings, active, context, preview, location) : readOnlyIssueGroup(warnings, active, preview, location), { description: mutable ? `${warnings.length} 项；处置可追溯，但不阻断终稿确认。` : `${warnings.length} 项；终稿冻结后仅供追溯。` }) : null,
    view.deck ? comparePanel(deckView, context) : null,
  ], [
    section("检查摘要", report ? [
      badge(report.stale ? "报告过期" : report.passed ? "检查通过" : "需要处置", report.stale ? "warning" : report.passed ? "success" : "danger"),
      metadataList([["报告 hash", shortHash(report.hash)], ["候选 hash", shortHash(report.deck_hash)], ["检查范围", report.metadata?.scope || "full"], ["修复轮次", report.metadata?.round ?? 0], ["问题总数", issues.length], ["未处置", view.unresolved?.length || 0]]),
    ] : [badge("尚未检查", "warning"), element("p", { className: "muted", text: "生成全稿后执行独立检查。" })]),
    section("终稿策略", [badge("检查可跳过", "primary"), element("p", { className: "muted", text: "未检查、报告过期或仍有遗留问题时，确认框会明确记录状态，但不会阻断确定终稿。" })]),
    section("全稿版本", [history, historyMessage]),
    section("检查历史", reportHistory(view)),
  ]);
}

function frozenPanel(context) {
  return section("终稿已冻结", [
    element("div", { className: "notice notice--success" }, [element("strong", { text: "当前候选已进入交付阶段" }), element("p", { text: "历史自检内容保持只读；修改、回退、复检和处置入口已关闭。需要调整时，请在交付完成后显式派生新候选。" })]),
    button("返回交付", { href: `/tasks/${encodeURIComponent(context.taskId)}?stage=delivery`, kind: "primary" }),
  ]);
}

function finalizePanel(view, context) {
  const message = actionMessage();
  const report = view.report;
  const status = !report ? "未检查" : report.stale ? "报告对应旧版本" : report.passed ? "检查通过" : `仍有 ${view.unresolved?.length || 0} 项未处置`;
  const finalize = button("确定终稿", { kind: "primary", disabled: !view.deck, reason: "请先生成全稿", mutates: true });
  finalize.addEventListener("click", () => confirmationDialog({
    title: "确认当前版本为终稿",
    description: `候选 ${shortHash(view.deck?.hash)} · 检查状态：${status}。确定后将冻结该版本并进入交付。`,
    confirmLabel: "确定终稿并前往交付",
    onConfirm: async () => {
      await runAction({ buttonNode: finalize, region: message, busyLabel: "正在冻结终稿…", action: () => api.finalizeDeck(context.taskId, { deck_hash: view.deck.hash, source: "review", actor: "user" }), success: "终稿已冻结。" });
      return () => context.goTo("delivery");
    },
  }));
  return section("确定终稿", [element("p", { text: `当前检查状态：${status}` }), finalize, message], { description: "自检、人工 Prompt 修改与问题处置均为可选；该操作始终绑定当前候选 hash。", className: "finalize-actions" });
}

function inspectionControls(view, context) {
  const message = actionMessage();
  const mode = element("select", { className: "select", id: "inspection-mode" }, [
    element("option", { value: "manual", text: "manual · 人工审核", selected: view.state.mode === "manual" }),
    element("option", { value: "auto", text: "auto · 有界自动修复", selected: view.state.mode === "auto" }),
  ]);
  const rounds = element("input", { className: "input", id: "inspection-rounds", type: "number", min: 0, max: 10, value: 2 });
  const slides = element("input", { className: "input", id: "inspection-slides", placeholder: "留空为全检；或 slide-2, slide-4" });
  const saveMode = button("保存模式", { kind: "secondary", mutates: true });
  saveMode.addEventListener("click", () => runAction({ buttonNode: saveMode, region: message, action: () => api.setInspectionMode(context.taskId, mode.value), success: "检查模式将在下一项动作生效。", refresh: context.refresh }).catch(() => {}));
  const run = button("执行独立检查", { kind: "primary", mutates: true, requiresRuntime: true });
  run.addEventListener("click", () => context.startJob("inspection.run", { max_rounds: Number(rounds.value), affected_slide_ids: parseSlideIds(slides.value) }, { buttonNode: run, region: message }));
  return section("检查设置", [
    element("div", { className: "form-grid" }, [field("检查模式", mode), field("最大自动修复轮数", rounds), field("增量检查页面", slides)]),
    element("div", { className: "button-row" }, [saveMode, run]),
    message,
  ], { description: "manual 始终等待人工审核；auto 达到轮次上限后同样等待人工。" });
}

function issueGroup(issues, active, context, preview, location) {
  if (!issues.length) return element("div", { className: "empty-state empty-state--compact" }, [element("p", { text: "本组暂无问题。" })]);
  return element("div", { className: "issue-list" }, issues.map((issue) => issueCard(issue, active.get(issue.issue_id), context, preview, location, issues)));
}

function readOnlyIssueGroup(issues, active, preview, location) {
  if (!issues.length) return element("div", { className: "empty-state empty-state--compact" }, [element("p", { text: "本组暂无问题。" })]);
  return element("div", { className: "issue-list" }, issues.map((issue) => {
    const disposition = active.get(issue.issue_id);
    return element("article", { className: `issue-card issue-card--${issue.severity}` }, [
      element("div", { className: "issue-card__header" }, [
        element("div", {}, [badge(issue.severity === "blocker" ? "阻断" : "警告", issue.severity === "blocker" ? "danger" : "warning"), element("strong", { text: issue.message })]),
        button("定位", { kind: "ghost", onClick: () => locateIssue(issue, preview, location) }),
      ]),
      metadataList([["级别", issue.level], ["位置", issue.slide_id ? `${issue.slide_id}${issue.element_id ? ` / ${issue.element_id}` : ""}` : "整稿"], ["问题 code", issue.code], ["证据", issue.evidence], ["建议", issue.suggestion]]),
      disposition ? element("div", { className: "notice" }, [element("strong", { text: `已处置：${disposition.action}` }), element("p", { text: disposition.rationale })]) : element("p", { className: "muted", text: "终稿确认时未记录处置。" }),
    ]);
  }));
}

function issueCard(issue, disposition, context, preview, location, group) {
  const message = actionMessage();
  const action = element("select", { className: "select", id: `issue-${issue.issue_id}-action` }, [
    element("option", { value: "agent_fix", text: "Agent 修复", selected: disposition?.action === "agent_fix" }),
    element("option", { value: "manual", text: "手工已处理", selected: disposition?.action === "manual" }),
    element("option", { value: "waive", text: "接受 / 豁免", selected: disposition?.action === "waive" }),
    element("option", { value: "defer", text: "暂不处理", selected: disposition?.action === "defer" }),
  ]);
  const rationale = element("input", { className: "input", id: `issue-${issue.issue_id}-rationale`, value: disposition?.rationale || "", placeholder: "说明处置依据（豁免时必填）" });
  const save = button("保存处置", { kind: "secondary", mutates: true });
  save.addEventListener("click", () => {
    if (action.value === "agent_fix") {
      context.startJob("inspection.fix", { issue_id: issue.issue_id, rationale: rationale.value }, { buttonNode: save, region: message });
      return;
    }
    runAction({ buttonNode: save, region: message, action: () => api.disposeIssue(context.taskId, issue.issue_id, { action: action.value, rationale: rationale.value, actor: "user" }), success: "问题处置已记录。", refresh: context.refresh }).catch(() => {});
  });
  const batch = button("处置同 code 问题", { kind: "ghost", mutates: true });
  const updateBatch = () => { batch.disabled = action.value === "agent_fix"; batch.title = batch.disabled ? "Agent 修复需逐项执行" : ""; };
  action.addEventListener("change", updateBatch);
  updateBatch();
  batch.addEventListener("click", () => {
    const ids = group.filter((item) => item.code === issue.code).map((item) => item.issue_id);
    runAction({ buttonNode: batch, region: message, action: () => api.disposeIssues(context.taskId, { issue_ids: ids, action: action.value, rationale: rationale.value }), success: `已处置 ${ids.length} 个同类问题。`, refresh: context.refresh }).catch(() => {});
  });
  const locate = button("定位", { kind: "ghost", onClick: () => locateIssue(issue, preview, location) });
  return element("article", { className: `issue-card issue-card--${issue.severity}` }, [
    element("div", { className: "issue-card__header" }, [
      element("div", {}, [badge(issue.severity === "blocker" ? "阻断" : "警告", issue.severity === "blocker" ? "danger" : "warning"), element("strong", { text: issue.message })]),
      locate,
    ]),
    metadataList([["级别", issue.level], ["位置", issue.slide_id ? `${issue.slide_id}${issue.element_id ? ` / ${issue.element_id}` : ""}` : "整稿"], ["问题 code", issue.code], ["证据", issue.evidence], ["建议", issue.suggestion]]),
    disposition ? element("div", { className: "notice" }, [element("strong", { text: `已处置：${disposition.action}` }), element("p", { text: disposition.rationale })]) : null,
    element("div", { className: "form-grid" }, [field("处置动作", action), field("处置依据", rationale)]),
    element("div", { className: "button-row" }, [save, batch]),
    message,
  ]);
}

function locateIssue(issue, wrapper, location) {
  const frame = wrapper?.querySelector("iframe");
  if (!frame || !issue.slide_id) {
    frame?.contentWindow?.scrollTo(0, 0);
    location.textContent = "已定位：整稿一致性问题。";
    return;
  }
  try {
    const doc = frame.contentDocument;
    doc?.querySelectorAll("[data-inspection-highlight]").forEach((node) => {
      node.style.outline = node.dataset.inspectionOutline || "";
      node.removeAttribute("data-inspection-highlight");
      delete node.dataset.inspectionOutline;
    });
    const slide = doc?.querySelector(`[data-slide-id="${CSS.escape(issue.slide_id)}"], #${CSS.escape(issue.slide_id)}`);
    const target = issue.element_id ? slide?.querySelector(`[data-element-id="${CSS.escape(issue.element_id)}"]`) : slide;
    if (!target) throw new Error("not found");
    target.dataset.inspectionOutline = target.style.outline;
    target.dataset.inspectionHighlight = "true";
    target.style.outline = "4px solid #dc2626";
    target.scrollIntoView({ block: "center" });
    location.textContent = `已定位：${issue.slide_id}${issue.element_id ? ` / ${issue.element_id}` : ""}`;
  } catch (_error) {
    location.textContent = "定位失败：当前预览中未找到目标，请检查报告是否过期。";
  }
}

function activeDispositions(dispositions) {
  const map = new Map();
  dispositions.filter((item) => !item.stale).forEach((item) => map.set(item.issue_id, item));
  return map;
}

function reportHistory(view) {
  if (!view.reports?.length) return element("p", { className: "muted", text: "尚无检查历史。" });
  return element("ol", { className: "compact-list" }, view.reports.slice().reverse().map((item) => element("li", {}, [element("strong", { text: shortHash(item.hash) }), element("small", { className: "muted", text: `轮次 ${item.metadata?.round ?? 0} · ${item.metadata?.scope || "full"}` })])));
}
