import { api } from "../api.js?v=2026.08.20.141243404257";
import { badge, button, confirmationDialog, element, field, formatTime, metadataList, shortHash } from "../components/index.js?v=2026.08.20.141243404257";
import { actionMessage, parseSlideIds, runAction, section, stageGrid } from "./shared.js?v=2026.08.20.141243404257";

export async function render(context) {
  const [view, deckView] = await Promise.all([
    api.delivery(context.taskId, context.controller),
    api.deck(context.taskId, context.controller),
  ]);
  context.assertCurrent();
  return deliveryStage(view, deckView.deck, context);
}

function deliveryStage(view, deck, context) {
  const latest = view.latest;
  const finalization = view.finalization;
  const completed = view.state.status === "completed";
  const confirmMessage = actionMessage();
  const confirm = button(completed ? "离线包已写入" : "将离线包写入工程文件夹", {
    kind: "primary",
    disabled: completed || !deck || !finalization,
    reason: "请先在全稿或自检与修改页确定终稿",
    mutates: true,
  });
  confirm.addEventListener("click", () => confirmationDialog({
    title: "写入不可变离线包",
    description: `终稿 ${shortHash(finalization?.deck_hash)} 将写入任务工程目录。网络图片会先下载、校验并改写为包内相对资源；失败时不会产生伪完成目录。`,
    confirmLabel: "开始写入并校验",
    onConfirm: async () => context.startJob("delivery.publish", {}, { buttonNode: confirm, region: confirmMessage }),
  }));

  return stageGrid([
    section("离线交付", [
      element("div", { className: "delivery-candidate" }, [
        element("div", {}, [element("p", { className: "eyebrow", text: "已确定终稿" }), element("h2", { text: deck && finalization ? `全稿 v${deck.version}` : "尚未确定终稿" })]),
        finalization ? badge(shortHash(finalization.deck_hash), "success") : badge("缺少终稿", "danger"),
      ]),
      element("div", { className: "gate-grid" }, [
        gateItem("终稿冻结", Boolean(finalization), finalization ? "已绑定精确 deck hash" : "待确定"),
        gateItem("检查状态", Boolean(finalization), inspectionStatus(finalization?.inspection_status)),
        gateItem("资源本地化", completed, completed ? "已校验" : "写包时执行"),
        gateItem("工程写入", completed, completed ? "已完成" : "等待用户"),
      ]),
      finalization?.unresolved_issue_count ? element("div", { className: "notice notice--warning" }, [element("strong", { text: `终稿带有 ${finalization.unresolved_issue_count} 项遗留问题` }), element("p", { text: "该事实会写入结果摘要，但不会阻止离线交付。" })]) : null,
      confirm,
      confirmMessage,
    ], { description: "写入使用后台 Job、临时目录与原子发布；刷新页面可恢复进度，同一终稿重复执行保持幂等。" }),
    latest ? derivePanel(latest, context) : section("交付后派生", element("p", { className: "muted", text: "完成首次交付后，可从不可变历史版本派生新的候选。" })),
    section("不可变交付历史", deliveryHistory(view.deliveries || []), { description: "每次交付记录终稿 hash、文件清单、本地化结果与检查摘要。" }),
  ], [
    section("交付状态", [
      badge(completed ? "已完成" : "等待写入", completed ? "success" : "warning"),
      metadataList([["终稿", shortHash(finalization?.deck_hash)], ["终稿记录", shortHash(finalization?.hash)], ["最近交付", shortHash(latest?.hash)], ["交付次数", view.deliveries?.length || 0], ["任务修订", view.state.revision]]),
    ]),
    section("终稿摘要", finalization ? [
      badge(inspectionStatus(finalization.inspection_status), ["passed", "issues_disposed"].includes(finalization.inspection_status) ? "success" : "warning"),
      metadataList([["来源页面", finalization.source === "review" ? "自检与修改" : "全稿"], ["遗留问题", finalization.unresolved_issue_count], ["其中阻断", finalization.blocking_issue_count]]),
    ] : [badge("尚未确定", "warning"), button("返回全稿", { href: `/tasks/${encodeURIComponent(context.taskId)}?stage=deck`, kind: "secondary" })]),
  ]);
}

function inspectionStatus(status) {
  return ({ unchecked: "未执行检查", stale: "检查报告对应旧版本", passed: "检查通过", issues_disposed: "问题已全部处置", issues_remaining: "带遗留问题确认" })[status] || "未记录";
}

function derivePanel(latest, context) {
  const message = actionMessage();
  const prompt = element("textarea", { className: "textarea", id: "delivery-derive-prompt", placeholder: "例如：基于已交付版本，为董事会增加一页风险摘要" });
  const slides = element("input", { className: "input", id: "delivery-derive-slides", placeholder: "可选：slide-3, slide-5" });
  const derive = button("从该交付派生新候选", { kind: "primary", type: "submit", mutates: true, allowCompleted: true });
  const form = element("form", { onSubmit: async (event) => {
    event.preventDefault();
    const payload = { delivery_hash: latest.hash, prompt: prompt.value, slide_ids: parseSlideIds(slides.value) };
    await runAction({ buttonNode: derive, region: message, busyLabel: "正在派生…", action: () => api.deriveDelivery(context.taskId, payload), success: "已从交付版本创建新候选。", refresh: () => context.goTo("review") }).catch(() => {});
  } }, [field("派生要求", prompt, { hint: "派生会重新打开任务，不会修改已交付文件。" }), field("限定页面（可选）", slides), derive, message]);
  return section("交付后派生", form, { description: `来源：${latest.delivery_id} · ${shortHash(latest.hash)}` });
}

function deliveryHistory(deliveries) {
  if (!deliveries.length) return element("div", { className: "empty-state empty-state--compact" }, [element("p", { text: "尚无交付记录。" })]);
  return element("ol", { className: "delivery-list" }, deliveries.slice().reverse().map((item) => {
    const files = item.files || [];
    return element("li", { className: "delivery-item" }, [
      element("div", { className: "version-item__header" }, [element("strong", { text: item.delivery_id }), badge("不可变", "success")]),
      metadataList([["交付时间", formatTime(item.confirmed_at)], ["终稿 hash", shortHash(item.deck_hash)], ["交付记录 hash", shortHash(item.hash)], ["文件数", files.length]]),
      element("details", {}, [element("summary", { text: "查看文件摘要" }), element("ul", { className: "compact-list" }, files.map((name) => element("li", {}, [element("strong", { text: name }), element("small", { className: "muted", text: shortHash(item.metadata?.file_hashes?.[name]) })])))]),
    ]);
  }));
}

function gateItem(label, passed, detail) {
  return element("div", { className: `gate-item ${passed ? "gate-item--passed" : ""}` }, [badge(passed ? "完成" : "待处理", passed ? "success" : "warning"), element("strong", { text: label }), element("small", { className: "muted", text: detail })]);
}
