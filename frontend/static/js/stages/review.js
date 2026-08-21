import { api } from "../api.js?v=2026.08.21.075744095363";
import { badge, button, confirmationDialog, element, field, metadataList, shortHash, versionTimeline } from "../components/index.js?v=2026.08.21.075744095363";
import { actionMessage, parseSlideIds, runAction, section, stageGrid } from "./shared.js?v=2026.08.21.075744095363";
import { comparePanel, deckPreview, modificationPanel } from "./deck.js?v=2026.08.21.075744095363";

export async function render(context) {
  const [view, deckView, settings] = await Promise.all([api.inspection(context.taskId, context.controller), api.deck(context.taskId, context.controller), api.settings(context.controller)]);
  context.assertCurrent();
  return reviewStage(view, deckView, settings.values.review.default_max_rounds, context);
}

function reviewStage(view, deckView, defaultRounds, context) {
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
    mutable ? inspectionControls(view, defaultRounds, context) : null,
    mutable && view.deck ? modificationPanel(view.deck, context) : null,
    report?.stale ? section("检查报告已过期", element("div", { className: "notice notice--warning" }, [element("strong", { text: "当前全稿已变化" }), element("p", { text: "旧报告仍可追溯，若需最新质量结论请重新检查；也可以带该状态直接确定终稿。" })])) : null,
    report ? section("阻断问题", mutable ? issueGroup(blockers, active, context, preview, location) : readOnlyIssueGroup(blockers, active, preview, location), { description: mutable ? `${blockers.length} 项 · 按页面与根因分组；修复、记录处置或带遗留问题确定终稿。` : `${blockers.length} 项；终稿冻结后仅供追溯。` }) : null,
    report ? section("普通警告", mutable ? issueGroup(warnings, active, context, preview, location) : readOnlyIssueGroup(warnings, active, preview, location), { description: mutable ? `${warnings.length} 项 · 按页面与根因分组；处置可追溯，但不阻断终稿确认。` : `${warnings.length} 项；终稿冻结后仅供追溯。` }) : null,
    view.deck ? comparePanel(deckView, context) : null,
  ], [
    section("检查摘要", report ? [
      badge(report.stale ? "报告过期" : report.passed ? "检查通过" : "需要处置", report.stale ? "warning" : report.passed ? "success" : "danger"),
      metadataList([["报告 hash", shortHash(report.hash)], ["候选 hash", shortHash(report.deck_hash)], ["检查范围", report.metadata?.scope || "full"], ["修复轮次", report.metadata?.round ?? 0], ["问题总数", issues.length], ["未处置", view.unresolved?.length || 0]]),
    ] : [badge("尚未检查", "warning"), element("p", { className: "muted", text: "生成全稿后执行独立检查。" })]),
    section("终稿策略", [badge("发布前强制预检", "primary"), element("p", { className: "muted", text: "存在未处置阻断问题时禁止默认定稿，只能显式选择带风险定稿并留痕；未检查、报告过期或仅剩警告时仍可确定终稿并记录状态；发布离线包始终要求新鲜报告且阻断清零。" })]),
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
  const blockers = view.blocking_issues || [];
  const status = !report ? "未检查" : report.stale ? "报告对应旧版本" : report.passed ? "检查通过" : `仍有 ${view.unresolved?.length || 0} 项未处置`;
  const finalize = button("确定终稿", {
    kind: "primary",
    disabled: !view.deck || blockers.length > 0,
    reason: !view.deck ? "请先生成全稿" : `仍有 ${blockers.length} 项未处置阻断问题，默认定稿已禁止`,
    mutates: true,
  });
  finalize.addEventListener("click", () => confirmationDialog({
    title: "确认当前版本为终稿",
    description: `候选 ${shortHash(view.deck?.hash)} · 检查状态：${status}。确定后将冻结该版本并进入交付。`,
    confirmLabel: "确定终稿并前往交付",
    onConfirm: async () => {
      await runAction({ buttonNode: finalize, region: message, busyLabel: "正在冻结终稿…", action: () => api.finalizeDeck(context.taskId, { deck_hash: view.deck.hash, source: "review", actor: "user" }), success: "终稿已冻结。" });
      return () => context.goTo("delivery");
    },
  }));
  const children = [element("p", { text: `当前检查状态：${status}` })];
  if (blockers.length) {
    const rationale = element("textarea", { className: "textarea", id: "risk-finalize-rationale", placeholder: "说明接受剩余阻断问题的业务依据（必填，随终稿事实与交付元数据留痕）" });
    const risk = button("带风险定稿", { kind: "danger", disabled: true, reason: "请先填写风险依据", mutates: true });
    rationale.addEventListener("input", () => {
      const ready = rationale.value.trim().length > 0;
      risk.disabled = !ready;
      if (ready) risk.removeAttribute("aria-description");
      else risk.setAttribute("aria-description", "请先填写风险依据");
    });
    risk.addEventListener("click", () => confirmationDialog({
      title: "带风险定稿",
      description: `候选 ${shortHash(view.deck?.hash)} 仍有 ${blockers.length} 项未处置阻断问题。该选择会写入终稿事实并明确标注为“带风险终稿”；发布离线包仍要求阻断问题清零。`,
      confirmLabel: "我已了解风险，确认带风险定稿",
      danger: true,
      onConfirm: async () => {
        await runAction({ buttonNode: risk, region: message, busyLabel: "正在冻结带风险终稿…", action: () => api.finalizeDeck(context.taskId, { deck_hash: view.deck.hash, source: "review", actor: "user", allow_risk: true, risk_rationale: rationale.value.trim() }), success: "带风险终稿已冻结并留痕。" });
        return () => context.goTo("delivery");
      },
    }));
    children.push(element("div", { className: "notice notice--danger" }, [
      element("strong", { text: `仍有 ${blockers.length} 项未处置阻断问题，默认定稿已禁止` }),
      element("p", { text: "请先修复或处置这些问题；确需继续时只能显式选择带风险定稿并填写依据，该选择会写入终稿事实与交付元数据，不能与正常终稿等价展示。" }),
    ]));
    children.push(finalize, field("风险依据（带风险定稿必填）", rationale), risk, message);
  } else {
    children.push(finalize, message);
  }
  return section("确定终稿", children, { description: "自检、人工 Prompt 修改与问题处置均为可选；该操作始终绑定当前候选 hash。", className: "finalize-actions" });
}

function inspectionControls(view, defaultRounds, context) {
  const message = actionMessage();
  const mode = element("select", { className: "select", id: "inspection-mode" }, [
    element("option", { value: "manual", text: "manual · 人工审核", selected: view.state.mode === "manual" }),
    element("option", { value: "auto", text: "auto · 有界自动修复", selected: view.state.mode === "auto" }),
  ]);
  const rounds = element("input", { className: "input", id: "inspection-rounds", type: "number", min: 0, max: 10, value: defaultRounds });
  const slides = element("input", { className: "input", id: "inspection-slides", placeholder: "留空为全检；或 slide-2, slide-4" });
  const saveMode = button("保存模式", { kind: "secondary", mutates: true });
  saveMode.addEventListener("click", () => runAction({ buttonNode: saveMode, region: message, action: () => api.setInspectionMode(context.taskId, mode.value), success: "检查模式将在下一项动作生效。", refresh: context.refresh }).catch(() => {}));
  const run = button("执行独立检查", { kind: "primary", mutates: true, requiresRuntime: true });
  run.addEventListener("click", () => context.startJob("inspection.run", { max_rounds: Number(rounds.value), affected_slide_ids: parseSlideIds(slides.value) }, { buttonNode: run, region: message }));
  const geometricCodes = new Set(["content_out_of_bounds", "element_scroll_overflow"]);
  const geometric = (view.blocking_issues || []).filter((issue) => geometricCodes.has(issue.code));
  const autofit = geometric.length ? button(`确定性修复溢出（${geometric.length} 项）`, { kind: "secondary", mutates: true, requiresRuntime: true }) : null;
  autofit?.addEventListener("click", () => {
    runAction({ buttonNode: autofit, region: message, busyLabel: "正在自适应修复溢出…", action: () => api.autofitOverflow(context.taskId, {}), success: "溢出自适应完成，已基于修复后的候选重新检查。", refresh: context.refresh }).catch(() => {});
  });
  return section("检查设置", [
    element("div", { className: "form-grid" }, [field("检查模式", mode), field("最大自动修复轮数", rounds), field("增量检查页面", slides)]),
    element("div", { className: "button-row" }, [saveMode, run, autofit].filter(Boolean)),
    message,
  ], { description: "manual 始终等待人工审核；auto 达到轮次上限后同样等待人工。确定性溢出修复不消耗模型额度：在 Chromium 中测量并对越界/滚动溢出元素做有界缩放，随后立即复检。" });
}

function issueGroup(issues, active, context, preview, location) {
  if (!issues.length) return element("div", { className: "empty-state empty-state--compact" }, [element("p", { text: "本组暂无问题。" })]);
  return element("div", { className: "issue-list" }, groupIssues(issues).map((group, index) => issueGroupCard(group, active, context, preview, location, index)));
}

function groupIssues(issues) {
  const groups = new Map();
  issues.forEach((issue) => {
    const key = `${issue.slide_id || ""}::${issue.code}`;
    if (!groups.has(key)) groups.set(key, { slide: issue.slide_id, code: issue.code, severity: issue.severity, items: [] });
    groups.get(key).items.push(issue);
  });
  return [...groups.values()];
}

function groupDomKey(group) {
  const partition = group.severity === "blocker" ? "blocker" : "warning";
  const sanitize = (value) => String(value ?? "").replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "deck";
  // 可读前缀只做定位锚点，清洗不是一一映射（如 "text overflow" 与 "text/overflow" 同形）；
  // 唯一性由未清洗原始 (severity, slide_id, code) 的稳定摘要保证。
  const digest = stableDigest([partition, String(group.slide ?? ""), String(group.code ?? "")]);
  return `${partition}-${sanitize(group.slide)}-${sanitize(group.code)}-${digest}`;
}

function stableDigest(parts) {
  const text = JSON.stringify(parts);
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let index = 0; index < text.length; index += 1) {
    const char = text.charCodeAt(index);
    h1 = Math.imul(h1 ^ char, 2654435761);
    h2 = Math.imul(h2 ^ char, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return `${(h2 >>> 0).toString(36)}-${(h1 >>> 0).toString(36)}`;
}

function issueGroupCard(group, active, context, preview, location, index) {
  const message = actionMessage();
  const disposed = group.items.filter((issue) => ["manual", "waive", "resolve"].includes(active.get(issue.issue_id)?.action));
  const domKey = groupDomKey(group);
  const action = element("select", { className: "select", id: `group-action-${domKey}` }, [
    element("option", { value: "manual", text: "手工已处理" }),
    element("option", { value: "waive", text: "接受 / 豁免" }),
    element("option", { value: "defer", text: "暂不处理" }),
  ]);
  const rationale = element("input", { className: "input", id: `group-rationale-${domKey}`, placeholder: "说明处置依据（豁免时必填）" });
  const save = button(`处置本组 ${group.items.length} 项`, { kind: "secondary", mutates: true });
  save.addEventListener("click", () => {
    runAction({ buttonNode: save, region: message, action: () => api.disposeIssues(context.taskId, { issue_ids: group.items.map((item) => item.issue_id), action: action.value, rationale: rationale.value }), success: `已处置 ${group.items.length} 个同类问题。`, refresh: context.refresh }).catch(() => {});
  });
  const summary = element("summary", { className: "issue-group__summary" }, [
    badge(group.severity === "blocker" ? "阻断" : "警告", group.severity === "blocker" ? "danger" : "warning"),
    element("strong", { text: `${group.slide || "整稿"} · ${group.code}` }),
    element("span", { className: "muted", text: `${group.items.length} 项${disposed.length ? ` · 已处置 ${disposed.length}` : ""}` }),
  ]);
  return element("details", { className: "issue-group", open: index === 0 ? "open" : null }, [
    summary,
    element("div", { className: "issue-group__items" }, group.items.map((issue) => issueCard(issue, active.get(issue.issue_id), context, preview, location))),
    element("div", { className: "form-grid" }, [field("处置动作", action), field("处置依据", rationale)]),
    element("div", { className: "button-row" }, [save]),
    message,
  ]);
}

function readOnlyIssueGroup(issues, active, preview, location) {
  if (!issues.length) return element("div", { className: "empty-state empty-state--compact" }, [element("p", { text: "本组暂无问题。" })]);
  return element("div", { className: "issue-list" }, groupIssues(issues).map((group, index) => element("details", { className: "issue-group", open: index === 0 ? "open" : null }, [
    element("summary", { className: "issue-group__summary" }, [
      badge(group.severity === "blocker" ? "阻断" : "警告", group.severity === "blocker" ? "danger" : "warning"),
      element("strong", { text: `${group.slide || "整稿"} · ${group.code}` }),
      element("span", { className: "muted", text: `${group.items.length} 项` }),
    ]),
    element("div", { className: "issue-group__items" }, group.items.map((issue) => {
      const disposition = active.get(issue.issue_id);
      return element("article", { className: `issue-card issue-card--${issue.severity}` }, [
        element("div", { className: "issue-card__header" }, [
          element("div", {}, [element("strong", { text: issue.message })]),
          button("定位", { kind: "ghost", onClick: () => locateIssue(issue, preview, location) }),
        ]),
        metadataList([["级别", issue.level], ["位置", issue.slide_id ? `${issue.slide_id}${issue.element_id ? ` / ${issue.element_id}` : ""}` : "整稿"], ["证据", issue.evidence], ["建议", issue.suggestion]]),
        disposition ? element("div", { className: "notice" }, [element("strong", { text: `已处置：${disposition.action}` }), element("p", { text: disposition.rationale })]) : element("p", { className: "muted", text: "终稿确认时未记录处置。" }),
      ]);
    })),
  ])));
}

function issueCard(issue, disposition, context, preview, location) {
  const message = actionMessage();
  const fix = button("Agent 修复", { kind: "ghost", mutates: true });
  fix.addEventListener("click", () => {
    context.startJob("inspection.fix", { issue_id: issue.issue_id, rationale: disposition?.rationale || "" }, { buttonNode: fix, region: message });
  });
  const locate = button("定位", { kind: "ghost", onClick: () => locateIssue(issue, preview, location) });
  return element("article", { className: `issue-card issue-card--${issue.severity}` }, [
    element("div", { className: "issue-card__header" }, [
      element("div", {}, [element("strong", { text: issue.message })]),
      element("div", { className: "button-row" }, [locate, fix]),
    ]),
    metadataList([["级别", issue.level], ["位置", issue.slide_id ? `${issue.slide_id}${issue.element_id ? ` / ${issue.element_id}` : ""}` : "整稿"], ["证据", issue.evidence], ["建议", issue.suggestion]]),
    disposition ? element("div", { className: "notice" }, [element("strong", { text: `已处置：${disposition.action}` }), element("p", { text: disposition.rationale })]) : null,
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
