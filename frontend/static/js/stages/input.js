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
  if (view.snapshot) primary.push(taskCard(view), resources(view));
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
    primary.push(section("阻断澄清", element("div", { className: "question-list" }, questions.map((question, index) => questionCard(question, answers[question.question_id], index, context))), {
      description: "每个问题单独提交；修改既有回答会让绑定旧资料的下游版本失效。",
    }));
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
    resources(view),
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

function questionCard(question, answer, index, context) {
  const message = actionMessage();
  const option = element("select", { className: "select", id: `question-${index}-option` }, [
    element("option", { value: "", text: "请选择回答" }),
    ...(question.options || []).map((value) => element("option", { value, text: value, selected: value === answer })),
    element("option", { value: "Other", text: "Other（自定义）", selected: Boolean(answer && !(question.options || []).includes(answer)) }),
  ]);
  const other = element("input", { className: "input", id: `question-${index}-other`, value: answer && !(question.options || []).includes(answer) ? answer : "", placeholder: "请输入自定义回答" });
  const toggleOther = () => { other.closest(".field").hidden = option.value !== "Other"; };
  option.addEventListener("change", toggleOther);
  const submit = button(answer ? "修改回答" : "提交回答", { kind: "primary", type: "submit", mutates: true });
  const form = element("form", { className: "question-card", "data-qid": question.question_id, onSubmit: async (event) => {
    event.preventDefault();
    const payload = { option: option.value };
    if (option.value === "Other") payload.other = other.value;
    await runAction({ buttonNode: submit, region: message, action: () => api.answerClarification(context.taskId, question.question_id, payload), success: "回答已保存。", refresh: context.refresh }).catch(() => {});
  } }, [
    element("div", { className: "question-card__title" }, [badge(question.blocking ? "阻断" : "建议", question.blocking ? "danger" : "warning"), element("h3", { text: question.prompt })]),
    field("选择回答", option),
    field("自定义回答", other),
    submit,
    message,
  ]);
  window.requestAnimationFrame(toggleOther);
  return form;
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

function resources(view) {
  const manifest = view.manifest || {};
  const items = manifest.resources || [];
  const warnings = manifest.warnings || [];
  return section("授权资源清单", [
    items.length ? element("ul", { className: "resource-list" }, items.map((item) => element("li", {}, [
      element("strong", { text: item.uri }),
      item.description ? element("p", { text: item.description }) : null,
      element("small", { className: "muted", text: `${item.media_type} · ${shortHash(item.content_hash)}` }),
    ]))) : element("p", { className: "muted", text: "当前快照没有授权图片资源。" }),
    warnings.length ? element("ul", { className: "warning-list" }, warnings.map((warning) => element("li", { text: `${warning.path || "资源"}：${WARNING_LABELS[warning.code] || warning.code}` }))) : null,
  ], { description: "仅清单内且 hash 匹配的资源可以进入样品和全稿。" });
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
