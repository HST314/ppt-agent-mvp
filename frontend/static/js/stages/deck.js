import { api } from "../api.js?v=2026.08.17.095744983694";
import { badge, button, element, field, metadataList, previewFrame, previewUrl, shortHash, versionTimeline } from "../components/index.js?v=2026.08.17.095744983694";
import { actionMessage, parseSlideIds, runAction, section, stageGrid } from "./shared.js?v=2026.08.17.095744983694";

export async function render(context) {
  const view = await api.deck(context.taskId, context.controller);
  context.assertCurrent();
  return deckStage(view, context);
}

function deckStage(view, context) {
  const deck = view.deck;
  const generationMessage = actionMessage();
  const generate = button(deck ? "重新生成完整演示稿" : "生成完整演示稿", { kind: "primary", mutates: true, requiresRuntime: true });
  generate.addEventListener("click", () => context.startJob("deck.generate", {}, { buttonNode: generate, region: generationMessage }));

  const previewRegion = element("div", { className: "deck-preview" });
  if (deck) previewRegion.append(deckPreview(deck, context.taskId));
  else previewRegion.append(element("div", { className: "empty-state empty-state--compact" }, [element("h2", { text: "尚未生成全稿" }), element("p", { text: "确认当前样品后即可通过后台 Job 生成整套 HTML 演示稿。" })]));

  const modify = deck ? modificationPanel(deck, context) : null;
  const compare = deck ? comparePanel(view, context) : null;
  const historyMessage = actionMessage();
  const history = versionTimeline(view.versions || [], "deck", {
    currentHash: deck?.hash,
    onPreview: (item) => {
      previewRegion.replaceChildren(deckPreview({ ...deck, metadata: item.metadata, hash: item.hash }, context.taskId));
    },
    onRollback: (item) => runAction({ region: historyMessage, action: () => api.rollbackDeck(context.taskId, item.hash), success: "历史全稿已复制为新的候选版本。", refresh: context.refresh }).catch(() => {}),
  });

  return stageGrid([
    section("全稿生成", [
      element("p", { text: "生成会保留已确认样品页，并发布一份尚未检查的完整候选稿。" }),
      generate,
      generationMessage,
    ], { description: "该操作可能超过 10 秒，可以安全离开页面并稍后恢复。" }),
    deck ? section("候选稿已发布", [
      badge(deck.metadata?.inspection_status === "pending" ? "等待独立检查" : "候选稿可检查", "warning"),
      element("p", { text: "候选生成与质量检查相互独立。请进入检查阶段，确认范围后再启动检查。" }),
      button("前往独立检查", { href: `/tasks/${encodeURIComponent(context.taskId)}?stage=review`, kind: "primary" }),
    ], { description: "进入检查页面不会自动调用模型；由你明确点击后才会执行。" }) : null,
    modify,
    section("演示稿预览", previewRegion, { description: deck ? `当前候选 v${deck.version} · ${Object.keys(deck.metadata?.page_hashes || {}).length} 页` : "固定比例沙箱预览" }),
    compare,
  ], [
    section("当前候选", deck ? [
      badge(`v${deck.version}`, "primary"),
      metadataList([["候选 hash", shortHash(deck.hash)], ["大纲 hash", shortHash(deck.outline_hash)], ["来源", deck.metadata?.source || "unknown"], ["操作者", deck.metadata?.operator || "system"], ["大纲一致", deck.metadata?.outline_consistent === false ? "否，需重新生成" : "是"]]),
      deck.metadata?.affected?.length ? element("p", { className: "muted", text: `受影响页面：${deck.metadata.affected.join("、")}` }) : null,
    ] : [badge("尚未生成", "warning")]),
    section("版本时间线", [history, historyMessage]),
  ]);
}

function modificationPanel(deck, context) {
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

function deckPreview(deck, taskId) {
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

function comparePanel(view, context) {
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
