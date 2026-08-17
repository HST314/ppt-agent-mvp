import { api } from "../api.js?v=2026.08.17.112846263255";
import { badge, button, confirmationDialog, element, field, formatTime, metadataList, shortHash } from "../components/index.js?v=2026.08.17.112846263255";
import { actionMessage, parseSlideIds, runAction, section, stageGrid } from "./shared.js?v=2026.08.17.112846263255";

export async function render(context) {
  const [view, deckView, inspection] = await Promise.all([
    api.delivery(context.taskId, context.controller),
    api.deck(context.taskId, context.controller),
    api.inspection(context.taskId, context.controller),
  ]);
  context.assertCurrent();
  return deliveryStage(view, deckView.deck, inspection, context);
}

function deliveryStage(view, deck, inspection, context) {
  const latest = view.latest;
  const completed = view.state.status === "completed";
  const confirmMessage = actionMessage();
  const confirm = button(completed ? "当前任务已完成" : "确认最终交付", { kind: "primary", disabled: completed || !deck || !inspection.delivery_allowed, reason: "需要当前候选通过检查且没有未处置阻断问题", mutates: true });
  confirm.addEventListener("click", () => confirmationDialog({
    title: "确认最终交付",
    description: `将当前候选 ${shortHash(deck?.hash)} 固化为不可变交付。后续修改只能从交付记录派生新候选。`,
    confirmLabel: "确认并生成离线交付",
    onConfirm: async () => runAction({ buttonNode: confirm, region: confirmMessage, busyLabel: "正在交付…", action: () => api.confirmDelivery(context.taskId, { deck_hash: deck.hash, actor: "user" }), success: "交付已确认并生成不可变离线文件。", refresh: context.refresh }),
  }));

  const blockers = inspection.blocking_issues || [];
  const unresolvedWarnings = (inspection.unresolved || []).filter((item) => item.severity !== "blocker");
  return stageGrid([
    section("最终交付确认", [
      element("div", { className: "delivery-candidate" }, [
        element("div", {}, [element("p", { className: "eyebrow", text: "当前候选" }), element("h2", { text: deck ? `全稿 v${deck.version}` : "尚无全稿候选" })]),
        deck ? badge(shortHash(deck.hash), "primary") : badge("缺少候选", "danger"),
      ]),
      element("div", { className: "gate-grid" }, [
        gateItem("候选版本", Boolean(deck), deck ? "已生成" : "未生成"),
        gateItem("有效检查", Boolean(inspection.report && !inspection.report.stale), inspection.report?.stale ? "报告过期" : inspection.report ? "报告有效" : "尚未检查"),
        gateItem("阻断问题", blockers.length === 0, blockers.length ? `${blockers.length} 项未处置` : "已清零"),
        gateItem("人工确认", completed, completed ? "已确认" : "等待用户"),
      ]),
      unresolvedWarnings.length ? element("div", { className: "notice notice--warning" }, [element("strong", { text: `${unresolvedWarnings.length} 项普通警告仍保留` }), element("p", { text: "普通警告不会阻断交付，但会写入交付摘要。" })]) : null,
      confirm,
      confirmMessage,
    ], { description: "最终交付必须由用户明确执行，并绑定当前候选 deck hash。" }),
    latest ? derivePanel(latest, context) : section("交付后派生", element("p", { className: "muted", text: "完成首次交付后，可从不可变历史版本派生新的候选。" })),
    section("不可变交付历史", deliveryHistory(view.deliveries || []), { description: "每次交付记录候选 hash、文件清单与摘要；历史内容不被后续修改覆盖。" }),
  ], [
    section("交付状态", [
      badge(completed ? "已完成" : "等待交付", completed ? "success" : "warning"),
      metadataList([["当前候选", shortHash(deck?.hash)], ["最近交付", shortHash(latest?.hash)], ["交付次数", view.deliveries?.length || 0], ["任务修订", view.state.revision]]),
    ]),
    section("检查摘要", [
      badge(inspection.delivery_allowed ? "门禁通过" : "门禁未通过", inspection.delivery_allowed ? "success" : "danger"),
      metadataList([["报告", shortHash(inspection.report?.hash)], ["阻断", blockers.length], ["未处置警告", unresolvedWarnings.length]]),
      !inspection.delivery_allowed ? button("返回检查", { href: `/tasks/${encodeURIComponent(context.taskId)}?stage=review`, kind: "secondary" }) : null,
    ]),
  ]);
}

function derivePanel(latest, context) {
  const message = actionMessage();
  const prompt = element("textarea", { className: "textarea", id: "delivery-derive-prompt", placeholder: "例如：基于已交付版本，为董事会增加一页风险摘要" });
  const slides = element("input", { className: "input", id: "delivery-derive-slides", placeholder: "可选：slide-3, slide-5" });
  const derive = button("从该交付派生新候选", { kind: "primary", type: "submit", mutates: true, allowCompleted: true });
  const form = element("form", { onSubmit: async (event) => {
    event.preventDefault();
    const payload = { delivery_hash: latest.hash, prompt: prompt.value, slide_ids: parseSlideIds(slides.value) };
    await runAction({ buttonNode: derive, region: message, busyLabel: "正在派生…", action: () => api.deriveDelivery(context.taskId, payload), success: "已从交付版本创建新候选，需要重新检查与确认。", refresh: () => context.goTo("deck") }).catch(() => {});
  } }, [field("派生要求", prompt, { hint: "派生会重新打开任务，不会修改已交付文件。" }), field("限定页面（可选）", slides), derive, message]);
  return section("交付后派生", form, { description: `来源：${latest.delivery_id} · ${shortHash(latest.hash)}` });
}

function deliveryHistory(deliveries) {
  if (!deliveries.length) return element("div", { className: "empty-state empty-state--compact" }, [element("p", { text: "尚无交付记录。" })]);
  return element("ol", { className: "delivery-list" }, deliveries.slice().reverse().map((item) => {
    const files = item.files || [];
    return element("li", { className: "delivery-item" }, [
      element("div", { className: "version-item__header" }, [element("strong", { text: item.delivery_id }), badge("不可变", "success")]),
      metadataList([["交付时间", formatTime(item.confirmed_at)], ["候选 hash", shortHash(item.deck_hash)], ["交付记录 hash", shortHash(item.hash)], ["文件数", files.length]]),
      element("details", {}, [element("summary", { text: "查看文件摘要" }), element("ul", { className: "compact-list" }, files.map((name) => element("li", {}, [element("strong", { text: name }), element("small", { className: "muted", text: shortHash(item.metadata?.file_hashes?.[name]) })]))) ]),
    ]);
  }));
}

function gateItem(label, passed, detail) {
  return element("div", { className: `gate-item ${passed ? "gate-item--passed" : ""}` }, [badge(passed ? "通过" : "待处理", passed ? "success" : "warning"), element("strong", { text: label }), element("small", { className: "muted", text: detail })]);
}
