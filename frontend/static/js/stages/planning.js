import { api } from "../api.js?v=2026.08.19.043945581370";
import { badge, button, element, field, formatTime, metadataList, shortHash, versionTimeline } from "../components/index.js?v=2026.08.19.043945581370";
import { actionMessage, codeBlock, draftGuard, parseSlideIds, runAction, section, stageGrid } from "./shared.js?v=2026.08.19.043945581370";

export async function render(context) {
  const view = await api.planning(context.taskId, context.controller);
  context.assertCurrent();
  const kind = context.selected.id;
  return planningStage(view, kind, context);
}

function planningStage(view, kind, context) {
  const document = view[kind];
  const markdown = document?.markdown || "";
  const message = actionMessage();
  const editor = element("textarea", {
    className: "textarea markdown-editor",
    id: `${kind}-markdown`,
    value: markdown,
    placeholder: kind === "narrative" ? "生成或直接输入叙事结构 Markdown" : "生成或直接输入逐页大纲 Markdown",
  });
  draftGuard(editor, markdown, context);
  const preview = codeBlock(markdown, `${stageLabel(kind)} Markdown 预览`);
  editor.addEventListener("input", () => { preview.textContent = editor.value; });
  const summary = element("input", { className: "input", id: `${kind}-summary`, value: "直接编辑", maxLength: 160 });
  const save = button("保存为新版本", { kind: "secondary", type: "submit", mutates: true });
  const saveForm = element("form", { className: "editor-actions", onSubmit: async (event) => {
    event.preventDefault();
    const action = kind === "narrative" ? api.editNarrative : api.editOutline;
    await runAction({
      buttonNode: save,
      region: message,
      busyLabel: "正在保存…",
      action: () => action(context.taskId, { markdown: editor.value, summary: summary.value || "直接编辑" }),
      success: "已保存为新的权威版本，下游失效范围由服务端重新计算。",
      refresh: (result) => { context.setDirty(false); return context.refresh(result); },
    }).catch(() => {});
  } }, [field("版本摘要", summary, { hint: "简要说明本次直接编辑的目的。" }), save]);

  const generate = generationPanel(kind, document, context);
  const confirmMessage = actionMessage();
  const confirm = button(kind === "narrative" ? "确认当前叙事结构" : "确认当前逐页大纲", { kind: "primary", disabled: !document, reason: "请先生成或保存内容", mutates: true });
  editor.addEventListener("input", () => {
    const dirty = editor.value !== markdown;
    confirm.disabled = dirty || !document;
    confirm.title = dirty ? "请先保存当前编辑内容，再确认服务端新版本" : "";
  });
  confirm.addEventListener("click", async () => {
    const action = kind === "narrative" ? api.confirmNarrative : api.confirmOutline;
    await runAction({ buttonNode: confirm, region: confirmMessage, busyLabel: "正在确认…", action: () => action(context.taskId), success: "当前版本已确认。", refresh: context.refresh }).catch(() => {});
  });

  const currentHash = document?.hash;
  const history = versionTimeline(view.versions || [], kind, {
    currentHash,
    onPreview: async (item) => {
      const version = await api.version(context.taskId, item.hash, context.controller);
      const content = JSON.parse(version.content);
      preview.textContent = content.markdown || "";
      preview.setAttribute("aria-label", `历史版本 ${shortHash(item.hash)} 预览`);
    },
    onRollback: (item) => rollback(kind, item, context, message),
  });

  const editorSection = section(`${stageLabel(kind)}编辑器`, [
    element("div", { className: "editor-grid" }, [
      element("div", {}, [field("Markdown 内容", editor, { hint: "直接编辑会创建权威新版本，不覆盖已有历史。" }), saveForm]),
      element("div", { className: "markdown-preview" }, [element("h3", { text: "纯文本预览" }), preview]),
    ]),
    message,
  ], { description: document ? `当前 v${document.version} · ${shortHash(document.hash)}` : "当前尚无内容版本。" });

  return stageGrid([
    generate,
    editorSection,
    planningCompare(view.versions || [], kind, context),
  ], [
    section("版本与确认", [
      document ? badge(`当前 v${document.version}`, "primary") : badge("尚未生成", "warning"),
      document ? metadataList([["内容 hash", shortHash(document.content_hash)], ["创建时间", formatTime(document.created_at)], ["来源", document.metadata?.action || "unknown"]]) : null,
      confirm,
      confirmMessage,
    ]),
    section("版本时间线", history, { description: "预览历史内容，或复制为新的当前版本。" }),
    document?.metadata?.invalidated ? section("失效范围", element("p", { text: JSON.stringify(document.metadata.invalidated) })) : null,
  ]);
}

function planningCompare(versions, kind, context) {
  const items = versions.filter((item) => item.kind === kind).slice().reverse();
  if (items.length < 2) return section("版本对比", element("p", { className: "muted", text: "至少保存两个版本后可对比 Markdown 内容。" }));
  const options = () => items.map((item, index) => element("option", { value: item.hash, text: `v${items.length - index} · ${item.metadata?.summary || shortHash(item.hash)}` }));
  const left = element("select", { className: "select", id: `${kind}-compare-left` }, options());
  const right = element("select", { className: "select", id: `${kind}-compare-right` }, options());
  right.selectedIndex = 1;
  const result = element("div", { className: "diff-result", role: "status", "aria-live": "polite" });
  const compare = button("对比版本", { kind: "secondary" });
  compare.addEventListener("click", async () => {
    await runAction({
      buttonNode: compare,
      region: result,
      action: async () => Promise.all([api.version(context.taskId, left.value, context.controller), api.version(context.taskId, right.value, context.controller)]),
      refresh: ([lhs, rhs]) => {
        const leftDoc = JSON.parse(lhs.content);
        const rightDoc = JSON.parse(rhs.content);
        result.replaceChildren(element("div", { className: "diff-columns" }, [codeBlock(leftDoc.markdown, "左版本 Markdown"), codeBlock(rightDoc.markdown, "右版本 Markdown")]));
      },
    }).catch(() => {});
  });
  return section("版本对比", [element("div", { className: "form-grid" }, [field("左版本", left), field("右版本", right)]), compare, result], { description: "并排查看两个不可变版本；对比不会改变当前内容。" });
}

function generationPanel(kind, document, context) {
  const message = actionMessage();
  const prompt = element("textarea", { className: "textarea", id: `${kind}-prompt`, placeholder: document ? "描述希望调整的结构或内容" : "可选：补充生成要求" });
  let slides = null;
  if (kind === "outline") slides = element("input", { className: "input", id: "outline-slide-ids", placeholder: "例如 slide-2, slide-4" });
  const generate = button(document ? `按要求修改${stageLabel(kind)}` : `生成${stageLabel(kind)}`, { kind: "primary", mutates: true, requiresRuntime: true });
  generate.addEventListener("click", async () => {
    const payload = { prompt: prompt.value || null };
    if (kind === "narrative") payload.scope = "all";
    if (slides) {
      const slideIds = parseSlideIds(slides.value);
      if (slideIds.length) payload.slide_ids = slideIds;
    }
    await context.startJob(`${kind}.generate`, payload, { buttonNode: generate, region: message });
  });
  return section(document ? `生成式修改${stageLabel(kind)}` : `生成${stageLabel(kind)}`, [
    field("修改 / 生成要求", prompt, { hint: "该操作通过后台 Job 执行，可以安全离开页面。" }),
    slides ? field("指定页面（可选）", slides, { hint: "留空为全量生成；指定页面必须同时填写修改要求。" }) : null,
    generate,
    message,
  ], { description: "进度与结果以后台持久化记录为准。" });
}

async function rollback(kind, item, context, message) {
  await runAction({
    region: message,
    action: () => api.rollbackPlanning(context.taskId, { kind, hash: item.hash }),
    success: "历史内容已复制为新的当前版本。",
    refresh: context.refresh,
  }).catch(() => {});
}

function stageLabel(kind) {
  return kind === "narrative" ? "叙事结构" : "逐页大纲";
}
