import { api } from "../api.js?v=2026.08.14.2";
import { badge, button, element, field, metadataList, previewFrame, previewUrl, shortHash, versionTimeline } from "../components/index.js?v=2026.08.14.2";
import { actionMessage, parseSlideIds, runAction, section, stageGrid } from "./shared.js?v=2026.08.14.2";

export async function render(context) {
  const [view, planning] = await Promise.all([
    api.samples(context.taskId, context.controller),
    api.planning(context.taskId, context.controller),
  ]);
  context.assertCurrent();
  return sampleStage(view, planning, context);
}

function sampleStage(view, planning, context) {
  const selection = view.selection;
  const sample = view.sample;
  const message = actionMessage();
  const selectedInput = element("input", {
    className: "input",
    id: "sample-slide-ids",
    value: (selection?.slide_ids || []).join(", "),
    placeholder: "例如 slide-1, slide-4",
  });
  const count = element("input", { className: "input", id: "sample-count", type: "number", min: 1, max: Math.max(1, planning.outline?.slide_ids?.length || 20), value: selection?.slide_ids?.length || 2 });
  const select = button(selection ? "更新样品页选择" : "推荐并保存样品页", { kind: "secondary", type: "submit", mutates: true });
  const selectionForm = element("form", { onSubmit: async (event) => {
    event.preventDefault();
    const ids = parseSlideIds(selectedInput.value);
    const payload = ids.length ? { slide_ids: ids } : { count: Number(count.value) };
    await runAction({ buttonNode: select, region: message, action: () => api.selectSamples(context.taskId, payload), success: "样品页选择已保存。", refresh: context.refresh }).catch(() => {});
  } }, [
    field("页面 ID（可选）", selectedInput, { hint: "留空时按样品页数自动推荐；填写时用逗号分隔。" }),
    field("自动推荐页数", count),
    select,
    message,
  ]);

  const generation = generationPanel(view, context);
  const previewRegion = element("div", { className: "preview-region" }, [
    previewFrame("", "当前 HTML 样品安全预览", { id: "sample-preview", src: sample ? previewUrl(context.taskId, sample.hash) : "" }),
  ]);
  const history = versionTimeline(view.versions || [], "sample", {
    currentHash: sample?.hash,
    onPreview: (item) => {
      const frame = previewRegion.querySelector("iframe");
      frame.src = previewUrl(context.taskId, item.hash);
      frame.title = `历史样品 ${shortHash(item.hash)} 安全预览`;
    },
  });
  const confirmMessage = actionMessage();
  const confirm = button("确认当前样品并进入全稿", { kind: "primary", disabled: !sample, reason: "请先生成样品", mutates: true });
  confirm.addEventListener("click", async () => {
    await runAction({ buttonNode: confirm, region: confirmMessage, busyLabel: "正在确认…", action: () => api.confirmSample(context.taskId), success: "当前样品已按 outline、selection 和内容 hash 绑定确认。", refresh: context.refresh }).catch(() => {});
  });
  const generateDeckMessage = actionMessage();
  const generateDeck = view.confirmation ? button("生成完整演示稿", { kind: "primary", mutates: true, onClick: () => context.startJob("deck.generate", {}, { buttonNode: generateDeck, region: generateDeckMessage }) }) : null;

  return stageGrid([
    section("样品页选择", selectionForm, {
      description: "选择应覆盖开场、核心内容或高风险页面；修改选择会使既有样品确认失效。",
    }),
    generation,
    section("固定比例安全预览", previewRegion, { description: sample ? `当前样品 v${sample.version}` : "生成后将在沙箱中显示。" }),
    sampleCompare(view.versions || [], context),
  ], [
    section("确认状态", [
      selection ? badge(`${selection.slide_ids.length} 个样品页`, "primary") : badge("尚未选择", "warning"),
      sample ? metadataList([["样品版本", `v${sample.version}`], ["样品 hash", shortHash(sample.hash)], ["大纲 hash", shortHash(view.outline_hash)], ["作用范围", sample.metadata?.scope || "global"]]) : null,
      view.confirmation ? badge("当前样品已确认", "success") : badge("等待人工确认", "warning"),
      confirm,
      confirmMessage,
      generateDeck,
      generateDeckMessage,
    ]),
    section("选择依据", selection ? selectionReasons(selection) : element("p", { className: "muted", text: "保存选择后显示推荐依据。" })),
    section("版本时间线", history),
  ]);
}

function sampleCompare(versions, context) {
  const items = versions.slice().reverse();
  if (items.length < 2) return section("样品版本对比", element("p", { className: "muted", text: "至少生成两个样品版本后可并排对比。" }));
  const options = () => items.map((item, index) => element("option", { value: item.hash, text: `v${items.length - index} · ${item.metadata?.summary || shortHash(item.hash)}` }));
  const left = element("select", { className: "select", id: "sample-compare-left" }, options());
  const right = element("select", { className: "select", id: "sample-compare-right" }, options());
  right.selectedIndex = 1;
  const result = element("div", { className: "preview-compare", role: "status", "aria-live": "polite" });
  const compare = button("对比样品版本", { kind: "secondary" });
  compare.addEventListener("click", () => {
    result.replaceChildren(
      previewFrame("", "左侧样品版本", { src: previewUrl(context.taskId, left.value) }),
      previewFrame("", "右侧样品版本", { src: previewUrl(context.taskId, right.value) }),
    );
  });
  return section("样品版本对比", [element("div", { className: "form-grid" }, [field("左版本", left), field("右版本", right)]), compare, result], { description: "两个版本均通过同一只读沙箱预览端点加载。" });
}

function generationPanel(view, context) {
  const sample = view.sample;
  const message = actionMessage();
  const prompt = element("textarea", { className: "textarea", id: "sample-prompt", placeholder: sample ? "例如：统一增加留白，突出标题层级" : "可选：说明样品视觉方向" });
  const scope = element("select", { className: "select", id: "sample-scope" }, [
    element("option", { value: "", text: "自动识别" }),
    element("option", { value: "global", text: "整组样品" }),
    element("option", { value: "page", text: "指定页面" }),
    element("option", { value: "element", text: "指定元素" }),
  ]);
  const slide = element("input", { className: "input", id: "sample-slide", placeholder: "slide-2" });
  const target = element("input", { className: "input", id: "sample-element", placeholder: "title" });
  const submit = button(sample ? "提交样品修改" : "生成 HTML 样品", { kind: "primary", mutates: true });
  submit.addEventListener("click", async () => {
    if (!sample) {
      await context.startJob("samples.generate", { prompt: prompt.value || null }, { buttonNode: submit, region: message });
      return;
    }
    const payload = { prompt: prompt.value, scope: scope.value || null, slide_id: slide.value || null, element_id: target.value || null };
    await context.startJob("samples.modify", payload, { buttonNode: submit, region: message });
  });
  return section(sample ? "生成式修改样品" : "生成 HTML 样品", [
    field("视觉要求", prompt, { hint: "生成和修改都通过可恢复后台 Job 执行。" }),
    sample ? element("div", { className: "form-grid" }, [field("作用范围", scope), field("页面 ID", slide), field("元素 ID", target)]) : null,
    submit,
    message,
    sample?.metadata?.understanding ? element("div", { className: "notice" }, [element("strong", { text: "本次修改理解" }), element("p", { text: JSON.stringify(sample.metadata.understanding) })]) : null,
  ]);
}

function selectionReasons(selection) {
  const reasons = selection.metadata?.reasons || {};
  return element("ul", { className: "resource-list" }, selection.slide_ids.map((slideId) => element("li", {}, [
    element("strong", { text: slideId }),
    element("p", { text: reasons[slideId] || "用户选择" }),
  ])));
}
