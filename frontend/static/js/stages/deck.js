import { api } from "../api.js?v=2026.08.19.043945581370";
import { badge, button, confirmationDialog, element, field, metadataList, previewFrame, previewUrl, shortHash } from "../components/index.js?v=2026.08.19.043945581370";
import { actionMessage, parseSlideIds, runAction, section, stageGrid } from "./shared.js?v=2026.08.19.043945581370";

export async function render(context) {
  const view = await api.deck(context.taskId, context.controller);
  context.assertCurrent();
  return deckStage(view, context);
}

function deckStage(view, context) {
  const deck = view.deck;
  const generationMessage = actionMessage();
  const generationActive = context.shell.active_jobs.some((job) => job.operation === "deck.generate");
  const generate = button("重试生成全稿", { kind: "secondary", disabled: Boolean(deck) || generationActive, reason: generationActive ? "全稿生成正在运行" : "已有可用全稿", mutates: true, requiresRuntime: true });
  generate.addEventListener("click", () => context.startJob("deck.generate", {}, { buttonNode: generate, region: generationMessage }));

  const previewRegion = element("div", { className: "deck-preview" });
  if (deck) previewRegion.append(deckPreview(deck, context.taskId));
  else previewRegion.append(element("div", { className: "empty-state empty-state--compact" }, [element("h2", { text: "尚未生成全稿" }), element("p", { text: "确认当前样品后即可通过后台 Job 生成整套 HTML 演示稿。" })]));

  const finalizeMessage = actionMessage();
  const finalize = button("确定终稿", { kind: "primary", disabled: !deck || generationActive, reason: generationActive ? "请等待当前生成完成" : "请先生成全稿", mutates: true });
  finalize.addEventListener("click", () => confirmationDialog({
    title: "将当前候选确定为终稿",
    description: `将全稿 v${deck?.version || "—"}（${shortHash(deck?.hash)}）冻结为交付版本。自检与人工修改是可选步骤。`,
    confirmLabel: "确定终稿并前往交付",
    onConfirm: async () => {
      await runAction({ buttonNode: finalize, region: finalizeMessage, busyLabel: "正在冻结终稿…", action: () => api.finalizeDeck(context.taskId, { deck_hash: deck.hash, source: "deck", actor: "user" }), success: "终稿已冻结。" });
      return () => context.goTo("delivery");
    },
  }));

  return stageGrid([
    !deck ? section("全稿生成", [
      badge(generationActive ? "正在后台生成" : "生成尚未完成", generationActive ? "primary" : "warning"),
      element("p", { text: generationActive ? "样品确认后已自动启动全稿生成；可以离开页面，进度会持续保存。" : "上次生成未成功完成，可保留已确认样品并重试。" }),
      generate,
      generationMessage,
    ]) : null,
    section("全稿浏览", [deck ? fullScreenAction(previewRegion) : null, previewRegion], { description: deck ? `当前候选 v${deck.version} · ${Object.keys(deck.metadata?.page_hashes || {}).length} 页` : "生成完成后在此全屏浏览" }),
    deck ? section("选择下一步", [
      element("p", { text: "可直接确定终稿，也可进入自检与修改；两条路径都不会强制要求检查通过。" }),
      element("div", { className: "button-row" }, [finalize, button("前往自检与修改", { href: `/tasks/${encodeURIComponent(context.taskId)}?stage=review`, kind: "secondary" })]),
      finalizeMessage,
    ], { className: "finalize-actions" }) : null,
  ], [
    section("当前候选", deck ? [
      badge(`v${deck.version}`, "primary"),
      metadataList([["候选 hash", shortHash(deck.hash)], ["大纲 hash", shortHash(deck.outline_hash)], ["来源", deck.metadata?.source || "unknown"], ["操作者", deck.metadata?.operator || "system"], ["大纲一致", deck.metadata?.outline_consistent === false ? "否，需重新生成" : "是"]]),
      deck.metadata?.affected?.length ? element("p", { className: "muted", text: `受影响页面：${deck.metadata.affected.join("、")}` }) : null,
    ] : [badge("尚未生成", "warning")]),
    section("流程说明", [badge("质检可选", "primary"), element("p", { className: "muted", text: "确定终稿后直接进入交付；如需版本回退、Prompt 修改或自检，请进入“自检与修改”。" })]),
  ]);
}

export function modificationPanel(deck, context) {
  const message = actionMessage();
  const prompt = element("textarea", { className: "textarea", id: "deck-prompt", placeholder: "例如：仅调整 slide-3 的信息层级，保持内容不变" });
  const type = element("select", { className: "select", id: "deck-change-type" }, [element("option", { value: "visual", text: "纯视觉修改" }), element("option", { value: "content", text: "内容 / 叙事修改" })]);
  const scope = element("select", { className: "select", id: "deck-scope" }, [element("option", { value: "global", text: "整稿" }), element("option", { value: "page", text: "指定页面" }), element("option", { value: "element", text: "指定元素" })]);
  const slides = element("input", { className: "input", id: "deck-slide-ids", placeholder: "slide-2, slide-4" });
  const target = element("input", { className: "input", id: "deck-element-id", placeholder: "title" });
  const submit = button("提交全稿修改", { kind: "primary", type: "submit", mutates: true, requiresRuntime: true });
  const form = element("form", { onSubmit: async (event) => {
    event.preventDefault();
    const payload = {
      prompt: prompt.value,
      change_type: type.value,
      scope: scope.value,
      slide_ids: scope.value === "global" ? [] : parseSlideIds(slides.value),
      element_id: scope.value === "element" ? (target.value || null) : null,
    };
    await context.startJob("deck.modify", payload, { buttonNode: submit, region: message });
  } }, [
    field("修改要求", prompt, { hint: "失败时最后成功版本仍保持可用；页面与元素范围必须明确。" }),
    element("div", { className: "form-grid" }, [field("修改类型", type), field("作用范围", scope), field("页面 ID", slides), field("元素 ID", target)]),
    submit,
    message,
  ]);
  return section("修改全稿", form, { description: "内容修改会同步产生新的权威大纲版本；视觉修改不会改变内容。" });
}

export function deckPreview(deck, taskId) {
  const ids = Object.keys(deck.metadata?.page_hashes || {});
  const wrapper = previewFrame("", "完整 HTML 演示稿安全预览", { id: "deck-preview-frame", allowInspection: true, src: previewUrl(taskId, deck.hash) });
  const frame = wrapper.querySelector("iframe");
  const status = element("p", { className: "muted", role: "status", "aria-live": "polite", text: ids.length ? `正在预览整稿，共 ${ids.length} 页。` : "预览已加载。" });
  const nav = element("div", { className: "slide-nav", role: "navigation", "aria-label": "演示稿页面导航" }, ids.map((slideId, index) => button(`${index + 1}`, { kind: "ghost", title: slideId, onClick: () => {
    try {
      frame.contentDocument?.querySelector(`[data-slide-id="${CSS.escape(slideId)}"], #${CSS.escape(slideId)}`)?.scrollIntoView({ block: "start" });
      status.textContent = `已定位：${slideId}`;
    } catch (_error) {
      status.textContent = `无法定位 ${slideId}，请浏览整稿预览。`;
    }
  } })));
  return element("div", {}, [nav, status, wrapper]);
}

function fullScreenAction(previewRegion) {
  const control = button("全屏浏览", { kind: "secondary" });
  control.addEventListener("click", async () => {
    const target = previewRegion.querySelector(".preview-aspect");
    if (target?.requestFullscreen) await target.requestFullscreen();
  });
  return element("div", { className: "button-row deck-toolbar" }, [control]);
}

export function comparePanel(view, context) {
  const versions = (view.versions || []).slice().reverse();
  if (versions.length < 2) return section("逐页版本对比", element("p", { className: "muted", text: "至少生成两个全稿版本后可逐页对比。" }));
  const options = () => versions.map((item, index) => element("option", { value: item.hash, text: `v${versions.length - index} · ${item.metadata?.summary || shortHash(item.hash)}` }));
  const left = element("select", { className: "select", id: "deck-compare-left" }, options());
  const right = element("select", { className: "select", id: "deck-compare-right" }, options());
  right.selectedIndex = 1;
  const result = element("div", { className: "diff-result", role: "status", "aria-live": "polite" });
  const compare = button("对比版本", { kind: "secondary" });
  compare.addEventListener("click", async () => {
    await runAction({
      buttonNode: compare,
      region: result,
      action: () => api.compareDeck(context.taskId, { left: left.value, right: right.value }),
      refresh: (data) => { result.replaceChildren(renderDiff(data)); },
    }).catch(() => {});
  });
  return section("逐页版本对比", [element("div", { className: "form-grid" }, [field("左版本", left), field("右版本", right)]), compare, result], { description: "按页面展示新增、删除、修改和未变化状态。" });
}

function renderDiff(data) {
  return element("div", { className: "diff-pages" }, data.pages.map((page) => element("article", { className: `diff-page diff-page--${page.status}` }, [
    element("div", { className: "version-item__header" }, [element("strong", { text: page.slide_id }), badge(diffLabel(page.status), page.status === "unchanged" ? "success" : "warning")]),
    page.status !== "unchanged" ? element("div", { className: "diff-columns" }, [element("pre", { text: page.left_html || "（无）", tabIndex: 0 }), element("pre", { text: page.right_html || "（无）", tabIndex: 0 })]) : null,
  ])));
}

function diffLabel(status) {
  return ({ added: "新增", removed: "删除", modified: "修改", unchanged: "未变化" })[status] || status;
}
