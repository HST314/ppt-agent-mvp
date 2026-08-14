import { api } from "../api.js";
import { badge, button, element, field, metadataList, shortHash } from "../components/index.js";
import { actionMessage, invalidationNotice, runAction, section, stageGrid } from "./shared.js";

const FIELD_LABELS = { goal: "演示目标", audience: "主要受众", topic: "核心主题" };
const WARNING_LABELS = {
  missing_sidecar: "缺少配套 Markdown 说明",
  empty_resource: "空文件，已跳过",
  duplicate_content: "内容与已有资源重复",
  invalid_image_content: "图片内容无效或已损坏，未纳入清单",
  resource_too_large: "资源超过大小限制，未纳入清单",
};

export async function render(context) {
  const view = await api.input(context.taskId, context.controller);
  context.assertCurrent();
  scheduleResourceReminder(view, context);
  return context.selected.id === "clarification" ? clarificationStage(view, context) : inputStage(view, context);
}

function inputStage(view, context) {
  const message = actionMessage();
  const format = element("select", { className: "select", id: "task-card-format" }, [
    element("option", { value: "markdown", text: "Markdown" }),
    element("option", { value: "json", text: "JSON" }),
  ]);
  const source = element("textarea", {
    className: "textarea textarea--editor",
    id: "task-card-source",
    placeholder: "演示目标：新品发布\n受众：管理层\n核心主题：年度增长",
  });
  const rebuild = element("input", { type: "checkbox", id: "rebuild-input", disabled: !view.snapshot || !["created", "clarification"].includes(view.state.stage) });
  const submit = button(view.snapshot ? "重建资料快照" : "导入并冻结资料", { kind: "primary", type: "submit", mutates: true });
  const form = element("form", { className: "stage-form", onSubmit: async (event) => {
    event.preventDefault();
    await runAction({
      buttonNode: submit,
      region: message,
      busyLabel: view.snapshot ? "正在重建…" : "正在导入…",
      action: () => api.importInput(context.taskId, { source: source.value, source_format: format.value, rebuild: rebuild.checked }),
      success: "资料已冻结，工作台将刷新到服务端最新状态。",
      refresh: context.refresh,
    }).catch(() => {});
  } }, [
    field("任务卡格式", format, { hint: "Markdown 更适合直接填写；JSON 适合结构化导入。" }),
    field("任务卡内容", source, { hint: "任务资料首次导入即冻结；只有逐页大纲前可显式重建。" }),
    element("label", { className: "check-row", htmlFor: "rebuild-input" }, [rebuild, element("span", { text: "显式重建快照并重新扫描授权资源" })]),
    submit,
    message,
  ]);

  const primary = [section(view.snapshot ? "更新任务资料" : "创建 / 导入任务卡", form, {
    description: view.snapshot ? "已有快照不会被静默覆盖；重建会明确失效下游内容。" : "填写目标、受众和主题，系统会扫描任务授权资源。",
  })];
  if (view.snapshot) primary.push(taskCard(view), resources(view, context));
  else primary.push(emptyInput());
  return stageGrid(primary, [snapshotCard(view), assumptionsCard(view)]);
}

function clarificationStage(view, context) {
  const clarification = view.clarification || {};
  const questions = questionDetails(clarification);
  const answers = clarification.answers || {};
  const primary = [];
  if (!view.snapshot) {
    primary.push(section("尚未导入任务资料", element("p", { text: "请先在“任务/资料”阶段导入任务卡，系统才会生成阻断澄清。" }), {
      actions: [button("返回任务/资料", { href: `/tasks/${encodeURIComponent(context.taskId)}?stage=created`, kind: "primary" })],
    }));
  } else if (!questions.length) {
    primary.push(section("无需额外澄清", element("div", { className: "success-panel" }, [
      badge("已确认", "success"),
      element("p", { text: "任务资料已完整，可直接进入叙事结构阶段。" }),
    ])));
  } else {
    const message = actionMessage();
    const submit = button("提交本轮回答", { kind: "primary", type: "submit", mutates: true });
    const form = element("form", { className: "clarification-form", onSubmit: async (event) => {
      event.preventDefault(); const submitted = {};
      for (const question of questions) {
        const selected = form.querySelector(`input[name="q-${question.question_id}"]:checked`);
        const custom = form.querySelector(`[data-other="${question.question_id}"]`);
        if (custom?.value.trim()) submitted[question.question_id] = { option: "Other", other: custom.value.trim() };
        else if (selected) submitted[question.question_id] = { option: selected.value };
      }
      await runAction({ buttonNode: submit, region: message, action: () => api.answerClarifications(context.taskId, submitted), success: "本轮回答已保存。", refresh: context.refresh }).catch(() => {});
    } }, [element("div", { className: "question-list" }, questions.map((question, index) => questionCard(question, answers[question.question_id], index))), submit, message]);
    primary.push(section("需求澄清", form, { description: "请集中回答本轮问题；每题也可在选项下方输入自己的回复。" }));
  }
  if (clarification.confirmed) primary.unshift(nextNarrative(context));
  primary.push(taskCard(view));
  const invalidated = clarification.invalidated || [];
  return stageGrid(primary, [
    section("澄清状态", [
      badge(clarification.confirmed ? "澄清已确认" : "等待回答", clarification.confirmed ? "success" : "warning"),
      metadataList([["已回答", `${Object.keys(answers).length} / ${questions.length}`], ["当前快照", shortHash(view.snapshot_hash)]]),
      invalidationNotice(invalidated),
    ]),
    resources(view, context),
  ]);
}

function nextNarrative(context) {
  const message = actionMessage();
  const start = button("生成叙事结构", { kind: "primary", mutates: true });
  start.addEventListener("click", () => context.startJob("narrative.generate", { prompt: null, scope: "all" }, { buttonNode: start, region: message }));
  return section("资料已可用于下一阶段", [
    element("p", { text: "阻断澄清已完成。生成叙事结构后，任务会进入需要人工确认的叙事阶段。" }),
    start,
    message,
  ]);
}

function questionCard(question, answer, index) {
  const custom = Boolean(answer && !(question.options || []).includes(answer));
  return element("article", { className: "question-card", "data-qid": question.question_id }, [
    element("div", { className: "question-card__title" }, [badge(question.blocking ? "阻断" : "建议", question.blocking ? "danger" : "warning"), element("h3", { text: question.prompt })]),
    element("div", { className: "question-options", "aria-label": "选择回答" }, (question.options || []).map((value, optionIndex) => element("label", { className: "question-option" }, [
      element("input", { type: "radio", name: `q-${question.question_id}`, value, checked: value === answer, id: `question-${index}-${optionIndex}` }),
      element("span", { text: value }),
    ]))),
    field("自己的回复（填写后优先采用）", element("input", { className: "input", "data-other": question.question_id, value: custom ? answer : "", placeholder: "也可以输入更准确的回答" })),
  ]);
}

function questionDetails(clarification) {
  const details = clarification.details || clarification.questions || [];
  return details.filter((item) => item && typeof item === "object");
}

function taskCard(view) {
  const card = view.task_card;
  if (!card) return emptyInput();
  const core = Object.entries(FIELD_LABELS).map(([key, label]) => [label, card[key] || "待澄清"]);
  const constraints = Object.entries(card.constraints || {});
  return section("任务卡", [
    metadataList(core),
    constraints.length ? element("div", {}, [element("h3", { text: "约束" }), metadataList(constraints)]) : null,
    (card.missing || []).length ? element("div", { className: "notice notice--warning" }, [element("strong", { text: "仍有阻断信息" }), element("p", { text: card.missing.map((key) => FIELD_LABELS[key] || key).join("、") })]) : element("p", { className: "success-message", text: "无缺失项，资料已可用于下一阶段。" }),
  ]);
}

function resources(view, context) {
  const manifest = view.manifest || {};
  const items = manifest.resources || [];
  const warnings = manifest.warnings || [];
  return section("授权资源清单", [
    items.length ? element("ul", { className: "resource-list" }, items.map((item) => element("li", {}, [
      element("strong", { text: item.uri }),
      item.description ? element("p", { text: item.description }) : null,
      element("small", { className: "muted", text: `${item.media_type} · ${shortHash(item.content_hash)}` }),
    ]))) : element("div", { className: "notice notice--warning" }, [
      element("strong", { text: "未检测到图片资源（可选）" }),
      element("p", { text: "这不会阻止澄清与生成；如需使用品牌图、产品图或数据截图，请先准备资源并重建快照。" }),
      button("查看资源准备方法", { kind: "secondary", onClick: () => openResourceReminder(view, context) }),
    ]),
    warnings.length ? element("ul", { className: "warning-list" }, warnings.map((warning) => element("li", { text: `${warning.path || "资源"}：${WARNING_LABELS[warning.code] || warning.code}` }))) : null,
  ], { description: "仅清单内且 hash 匹配的资源可以进入样品和全稿。" });
}

function scheduleResourceReminder(view, context) {
  if (!view.snapshot_hash || (view.manifest?.resources || []).length) return;
  const key = `ppt-agent:resource-reminder:${context.taskId}:${view.snapshot_hash}`;
  try {
    if (window.sessionStorage.getItem(key)) return;
    window.sessionStorage.setItem(key, "shown");
  } catch (_error) {
    // Storage can be unavailable in privacy modes; the reminder still works.
  }
  window.requestAnimationFrame(() => {
    if (document.querySelector("dialog[open]")) return;
    openResourceReminder(view, context);
  });
}

function openResourceReminder(view, context) {
  const dialogId = `resource-reminder-${context.taskId}`;
  document.getElementById(dialogId)?.remove();
  const resourcePath = `<数据目录>/${context.taskId}/resources/`;
  const close = (nextStage = null) => {
    dialog.close();
    dialog.remove();
    if (nextStage && context.selected.id !== nextStage) context.goTo(nextStage);
  };
  const prepare = button("返回准备资源", { kind: "secondary", onClick: () => close("created") });
  const continueWithoutImages = button("继续无图片", { kind: "primary", onClick: () => close(view.state?.stage === "clarification" ? "clarification" : null) });
  const dialog = element("dialog", {
    id: dialogId,
    "aria-labelledby": `${dialogId}-title`,
    "aria-describedby": `${dialogId}-description`,
  }, [
    element("div", { className: "dialog__body" }, [
      badge("资源可选", "warning"),
      element("h2", { id: `${dialogId}-title`, text: "没有检测到图片资源" }),
      element("p", { id: `${dialogId}-description`, text: "资源为空不是错误，也不会卡住流程。你可以继续生成纯文本演示稿，或先补充品牌图、产品图和数据截图。" }),
      element("ol", { className: "dialog__steps" }, [
        element("li", {}, ["将 PNG、JPG、WebP、GIF 或 SVG 放入 ", element("code", { text: resourcePath })]),
        element("li", {}, ["可为图片添加同名 Markdown 说明，例如 ", element("code", { text: "hero.png + hero.md" })]),
        element("li", { text: "回到“任务/资料”，勾选“显式重建快照”后重新导入。" }),
      ]),
    ]),
    element("div", { className: "dialog__actions" }, [prepare, continueWithoutImages]),
  ]);
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
  document.body.append(dialog);
  dialog.showModal();
  continueWithoutImages.focus();
}

function snapshotCard(view) {
  return section("输入冻结", view.snapshot ? [
    badge("已冻结", "success"),
    metadataList([["快照", shortHash(view.snapshot_hash)], ["创建时间", view.snapshot.created_at || "—"]]),
  ] : [badge("尚未导入", "warning"), element("p", { className: "muted", text: "导入后会生成不可静默变更的资料快照。" })]);
}

function assumptionsCard(view) {
  const defaults = view.task_card?.defaults || {};
  const assumptions = view.task_card?.assumptions || [];
  return section("默认值与假设", [
    metadataList([["语言", defaults.language || "zh-CN"], ["画布比例", defaults.aspect_ratio || "16:9"], ["样品页数", defaults.sample_count || 2]]),
    element("h3", { text: "显式假设" }),
    assumptions.length ? element("ul", {}, assumptions.map((item) => element("li", { text: item }))) : element("p", { className: "muted", text: "无显式假设。" }),
  ]);
}

function emptyInput() {
  return section("尚未导入任务卡", element("p", { className: "muted", text: "请先导入任务卡。冻结完成后，系统会展示任务信息、资源诊断和澄清问题。" }));
}
