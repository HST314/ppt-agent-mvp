import { api } from "../api.js?v=2026.08.20.152614537731";
import { badge, button, confirmationDialog, element, field, metadataList, shortHash } from "../components/index.js?v=2026.08.20.152614537731";
import { actionMessage, invalidationNotice, runAction, section, stageGrid } from "./shared.js?v=2026.08.20.152614537731";

const FIELD_LABELS = { goal: "演示目标", audience: "主要受众", topic: "核心主题" };
const WARNING_LABELS = {
  missing_sidecar: "缺少配套 Markdown 说明",
  empty_resource: "空文件，已跳过",
  duplicate_content: "内容与已有资源重复",
  invalid_image_content: "图片内容无效或已损坏，未纳入清单",
  resource_too_large: "资源超过大小限制，未纳入清单",
};

function frozenSourceText(source, format) {
  if (source === null || source === undefined) return "";
  if (format === "json" && typeof source === "object") return JSON.stringify(source, null, 2);
  return String(source);
}

export async function render(context) {
  const view = await api.input(context.taskId, context.controller);
  context.assertCurrent();
  if (context.selected.id !== "clarification") scheduleResourceReminder(view, context);
  return context.selected.id === "clarification" ? clarificationStage(view, context) : inputStage(view, context);
}

function inputStage(view, context) {
  const message = actionMessage();
  const hasSnapshot = Boolean(view.snapshot);
  const frozenFormat = hasSnapshot ? view.source_format : null;
  const frozenSource = hasSnapshot ? frozenSourceText(view.source, frozenFormat) : "";
  const format = element("select", { className: "select", id: "task-card-format" }, [
    element("option", { value: "markdown", text: "Markdown" }),
    element("option", { value: "json", text: "JSON" }),
  ]);
  const source = element("textarea", {
    className: "textarea textarea--editor",
    id: "task-card-source",
    placeholder: "演示目标：新品发布\n受众：管理层\n核心主题：年度增长",
  });
  if (hasSnapshot) {
    if (frozenFormat === "json" || frozenFormat === "markdown") format.value = frozenFormat;
    source.value = frozenSource;
  }
  const rebuild = element("input", { type: "checkbox", id: "rebuild-input", disabled: !view.snapshot || !["created", "clarification"].includes(view.state.stage) });
  const submit = button(view.snapshot ? "重建资料快照" : "导入并冻结资料", { kind: "primary", type: "submit", mutates: true, requiresVersionMatch: true });
  const gateHint = element("p", { className: "field__hint", id: "rebuild-gate-hint", role: "status" });
  const updateGate = () => {
    const enable = () => { if (submit.dataset.versionDisabled !== "true") submit.disabled = false; };
    if (!hasSnapshot) {
      enable();
      gateHint.textContent = "";
      return;
    }
    if (source.value === frozenSource && format.value === frozenFormat) {
      submit.disabled = true;
      gateHint.textContent = "资料未变化，无需重建；修改原文后才可提交。";
      return;
    }
    if (!rebuild.checked) {
      submit.disabled = true;
      gateHint.textContent = "资料已修改；请勾选“显式重建快照”后再提交。";
      return;
    }
    enable();
    gateHint.textContent = "";
  };
  source.addEventListener("input", updateGate);
  format.addEventListener("change", updateGate);
  rebuild.addEventListener("change", updateGate);
  const submitImport = () => runAction({
    buttonNode: submit,
    region: message,
    busyLabel: view.snapshot ? "正在重建…" : "正在导入…",
    action: () => api.importInput(context.taskId, { source: source.value, source_format: format.value, rebuild: rebuild.checked }),
    success: "资料已冻结，正在进入澄清阶段。",
    refresh: () => context.goTo(null),
    requiresVersionMatch: true,
  });
  const form = element("form", { className: "stage-form", onSubmit: async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    if (hasSnapshot) {
      confirmationDialog({
        title: "确认重建资料快照？",
        description: "重建将以新资料替换当前快照，以下下游内容会立即失效：当前澄清问题、已填写的答案、正在进行的澄清生成任务。确认后系统会重新扫描授权资源并重新生成澄清问题。",
        confirmLabel: "确认重建",
        danger: true,
        onConfirm: async () => {
          await submitImport().catch(() => {});
          updateGate();
        },
      });
      return;
    }
    await submitImport().catch(() => {});
    updateGate();
  } }, [
    view.state.mode === "quick" ? element("div", { className: "notice", role: "status" }, [
      element("strong", { text: `快速生成 · 严格 ${view.state.target_slide_count} 页` }),
      element("p", { text: "系统只询问阻断交付的问题；澄清完成后会自动生成并保存叙事结构、逐页大纲与样品，仍可在各阶段回看。" }),
    ]) : null,
    field("任务卡格式", format, { hint: "Markdown 更适合直接填写；JSON 适合结构化导入。" }),
    field("任务卡内容", source, { hint: hasSnapshot ? "已按当前冻结快照回填原始资料；修改后需显式重建。" : "任务资料首次导入即冻结；只有逐页大纲前可显式重建。" }),
    element("label", { className: "check-row", htmlFor: "rebuild-input" }, [rebuild, element("span", { text: "显式重建快照并重新扫描授权资源" })]),
    submit,
    gateHint,
    message,
  ]);
  updateGate();
  // 版本阻断解除后按当前输入重新评估业务闸；版本阻断状态由 app.js 独立打标，
  // enable() 已检查 data-version-disabled，mismatch 期间不会被本闸重新启用。
  document.addEventListener("versiongatechange", updateGate, { signal: context.controller.signal });

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
  const activeJob = (context.shell.active_jobs || []).find((job) => job.operation === "clarification.generate");
  const status = activeJob ? "generating" : (clarification.status || "ready");
  const primary = [];
  if (!view.snapshot) {
    primary.push(section("尚未导入任务资料", element("p", { text: "请先在“任务/资料”阶段导入任务卡，系统才会生成阻断澄清。" }), {
      actions: [button("返回任务/资料", { href: `/tasks/${encodeURIComponent(context.taskId)}?stage=created`, kind: "primary" })],
    }));
  } else if (status === "generating") {
    primary.push(clarificationGenerating(activeJob, clarification));
  } else if (status === "failed") {
    primary.push(clarificationFailed(clarification, context));
  } else if (status !== "ready") {
    primary.push(section("澄清状态暂不可用", element("div", { className: "notice notice--warning", role: "alert" }, [
      element("strong", { text: "服务返回了无法识别的澄清状态" }),
      element("p", { text: "为避免提前展示未完成的问题，当前不会渲染答题内容。请刷新后重试。" }),
      button("刷新状态", { kind: "primary", onClick: context.refresh }),
    ])));
  } else if (!questions.length) {
    primary.push(section("无需额外澄清", element("div", { className: "success-panel" }, [
      badge("已确认", "success"),
      element("p", { text: "任务资料已完整，可直接进入叙事结构阶段。" }),
    ])));
  } else {
    const message = actionMessage();
    const progress = element("strong", { className: "clarification-progress", role: "status", "aria-live": "polite" });
    const cards = questions.map((question, index) => questionCard(question, answers[question.question_id], index));
    const submit = button("提交答案并继续", { kind: "primary", type: "submit", mutates: true, requiresVersionMatch: true });
    const updateProgress = () => {
      const completed = cards.filter((card, index) => readAnswer(card, questions[index])).length;
      progress.textContent = `已完成 ${completed}/${questions.length}`;
    };
    cards.forEach((card) => card.addEventListener("answerchange", updateProgress));
    const form = element("form", { className: "clarification-form", onSubmit: async (event) => {
      event.preventDefault();
      const submitted = {};
      const missing = [];
      cards.forEach((card, index) => {
        const question = questions[index];
        const answer = readAnswer(card, question);
        setQuestionError(card, "");
        if (answer) submitted[question.question_id] = answer;
        else if (question.blocking) missing.push(card);
      });
      if (missing.length) {
        missing.forEach((card) => setQuestionError(card, "请回答此题后再提交本轮答案。"));
        missing[0].scrollIntoView({ block: "center" });
        missing[0].focus({ preventScroll: true });
        return;
      }
      await runAction({
        buttonNode: submit,
        region: message,
        busyLabel: "正在提交整轮回答…",
        action: () => api.answerClarifications(context.taskId, submitted),
        success: "本轮回答已保存，正在刷新任务状态。",
        refresh: context.refresh,
        requiresVersionMatch: true,
      }).catch(() => {});
    } }, [
      clarificationSource(clarification),
      element("div", { className: "question-list" }, cards),
      element("div", { className: "clarification-submit" }, [progress, submit]),
      message,
    ]);
    updateProgress();
    primary.push(section("需求澄清", form, { description: "请一次完成本轮所有阻断问题。每题可选择一个答案，或填写自己的答案。" }));
  }
  if (status === "ready" && clarification.confirmed) primary.unshift(nextNarrative(context));
  primary.push(taskCard(view));
  const invalidated = clarification.invalidated || [];
  return stageGrid(primary, [
    section("澄清状态", [
      clarificationStatusBadge(status, clarification),
      metadataList([
        clarificationRound(clarification) ? ["当前轮次", clarificationRound(clarification)] : null,
        status === "ready" ? ["已回答", `${Object.keys(answers).length} / ${questions.length}`] : null,
        activeJob ? ["生成任务", activeJob.job_id] : null,
        ["当前快照", shortHash(view.snapshot_hash)],
      ].filter(Boolean)),
      invalidationNotice(invalidated),
    ]),
    clarificationResources(view, context),
  ], "stage-grid--clarification");
}

function clarificationGenerating(job, clarification) {
  const round = clarification && Number.isInteger(clarification.round) && Number.isInteger(clarification.max_rounds)
    ? `第 ${clarification.round}/${clarification.max_rounds} 轮` : "本轮";
  return section("模型正在阅读任务卡", element("div", { className: "clarification-generating", role: "status", "aria-live": "polite", "aria-atomic": "true" }, [
    element("div", { className: "clarification-generating__header" }, [
      badge("AI 生成中", "primary"),
      element("p", { text: `正在结合原始任务卡、规范化字段、资源摘要和已答记录整理${round}问题。` }),
    ]),
    element("div", { className: "clarification-generating__skeleton", "aria-hidden": "true" }, [
      element("span", { className: "skeleton skeleton--title" }),
      element("span", { className: "skeleton skeleton--line" }),
      element("span", { className: "skeleton skeleton--line" }),
    ]),
    element("p", { className: "field__hint", text: job
      ? "生成任务已持久化，可以安全离开此页；完成后工作台会自动刷新。"
      : "模型完成前不会显示任何问题。工作台会从服务端重新读取最终结果。" }),
  ]), { description: "模型完成前不会展示问题，也不会自动切换到系统兜底题。" });
}

function clarificationFailed(clarification, context) {
  const error = clarification.error || {};
  const message = actionMessage();
  const advice = clarificationRecoveryAdvice(error);
  const retry = button("重新生成问题", { kind: "primary", mutates: true, requiresRuntime: true, onClick: () => {
    context.retryClarification({ buttonNode: retry, region: message });
  } });
  const fallback = button("使用系统兜底问题", { kind: "secondary", mutates: true, onClick: () => {
    confirmationDialog({
      title: "确认使用系统兜底问题？",
      description: "兜底问题来自确定性缺口检查，不是模型阅读任务卡后生成的内容。仅在暂时无法等待模型恢复时使用。",
      confirmLabel: "确认使用兜底问题",
      onConfirm: () => runAction({
        buttonNode: fallback,
        region: message,
        busyLabel: "正在启用兜底问题…",
        action: () => api.useFallbackClarification(context.taskId),
        success: "已按你的明确确认启用系统兜底问题。",
        refresh: context.refresh,
      }),
    });
  } });
  return section("问题生成失败", [
    element("div", { className: "clarification-failed", role: "alert" }, [
      badge("未生成问题", "danger"),
      element("p", { text: error.message || "模型未能完成本轮澄清问题生成，系统没有自动展示固定问题。" }),
      metadataList([
        ["错误代码", error.code || "clarification_generation_failed"],
        error.runtime_error_code ? ["运行时错误", error.runtime_error_code] : null,
        error.failed_check ? ["失败检查", runtimeCheckLabel(error.failed_check)] : null,
        error.probe_phase ? ["失败阶段", runtimePhaseLabel(error.probe_phase)] : null,
        Number.isInteger(error.tool_calls) ? ["工具调用数", String(error.tool_calls)] : null,
        error.underlying_code ? ["底层错误", error.underlying_code] : null,
        error.probe_id ? ["探测 ID", error.probe_id] : null,
        ["诊断 ID", error.diagnostic_id || "—"],
        ["Agent 审计 ID", error.agent_audit_id || "—"],
      ].filter(Boolean)),
      error.agent_audit_id ? copyValueButton("复制审计 ID", error.agent_audit_id) : null,
      error.probe_id ? copyValueButton("复制探测 ID", error.probe_id) : null,
    ]),
    element("div", { className: "clarification-failure-actions" }, [retry, fallback]),
    element("p", { className: "field__hint", text: advice }),
    message,
  ], { description: "模型失败后流程保持关闭，不会静默切换为固定问题。" });
}

function clarificationRecoveryAdvice(error) {
  const code = error.code || "clarification_generation_failed";
  const cause = error.runtime_error_code || code;
  if (["model_authentication_failed", "model_permission_denied", "model_not_found", "model_request_invalid"].includes(cause)) {
    return "这是确定性配置故障。请联系管理员修复模型凭据、权限、模型名或结构化输出配置，再从连接状态执行“重新检测模型”；不要连续重试。";
  }
  if (cause === "model_rate_limited") {
    const wait = error.retry_after_seconds ? `至少等待 ${error.retry_after_seconds} 秒后` : "等待限流窗口结束后";
    return `${wait}重新检测模型，确认恢复后再生成；不要连续点击重试。`;
  }
  if (cause === "model_upstream_unavailable") {
    return "上游模型服务暂时异常。请稍后重新检测，确认“模型可用”后再生成。";
  }
  if (["model_timeout", "model_connection_error", "gateway_unknown_result"].includes(cause)) {
    return "本次请求结果可能未知。请先使用审计 ID 核对供应商记录，再重新检测模型；不要直接重复提交。";
  }
  if (cause === "probe_tool_call_missing") {
    return "模型忽略了强制工具调用。请联系管理员确认模型支持函数调用与 tool_choice，切换到兼容模型后重新检测；不要连续重试。";
  }
  if (cause === "probe_tool_round_failed") {
    return "模型端点未完成工具结果回传。请联系管理员核对 Responses API 续轮格式或切换兼容端点，重新检测通过后再生成。";
  }
  if (cause === "probe_tool_final_invalid_output") {
    return "工具调用已完成，但模型最终输出未通过 JSON Schema。请联系管理员核对模型的结构化输出能力，修复后重新检测。";
  }
  if (code === "runtime_unavailable") {
    return "模型运行时未通过就绪检查。请在右上角设置中重新检测；仍失败时按运行时错误代码联系管理员。";
  }
  return "请先复制诊断信息并联系管理员核对运行日志；确认模型恢复后再重新生成。使用系统兜底问题仍需明确确认。";
}

function runtimeCheckLabel(check) {
  return ({ basic_response: "基础文本响应", strict_json_schema: "严格 JSON Schema", tool_round_trip: "工具调用与结果回传", capability_contract: "能力契约" })[check] || check;
}

function runtimePhaseLabel(phase) {
  return ({ basic_response: "基础响应", strict_json_schema: "结构化输出", tool_request: "请求工具调用", tool_result: "回传工具结果", tool_final_output: "工具轮最终输出" })[phase] || phase;
}

function copyValueButton(label, value) {
  const control = button(label, { kind: "ghost", onClick: async () => {
    try {
      await navigator.clipboard.writeText(value);
      control.textContent = "已复制";
      window.setTimeout(() => { control.textContent = label; }, 1800);
    } catch (_error) {
      control.textContent = "复制失败，请手动选择";
    }
  } });
  return control;
}

function clarificationStatusBadge(status, clarification) {
  if (status === "generating") return badge("模型生成中", "primary");
  if (status === "failed") return badge("生成失败", "danger");
  return badge(clarification.confirmed ? "澄清已确认" : "等待回答", clarification.confirmed ? "success" : "warning");
}

function nextNarrative(context) {
  const message = actionMessage();
  const start = button("生成叙事结构", { kind: "primary", mutates: true, requiresRuntime: true });
  start.addEventListener("click", () => context.startJob("narrative.generate", { prompt: null, scope: "all" }, { buttonNode: start, region: message }));
  const quick = context.shell.task.mode === "quick";
  return section(quick ? "快速流程已完成阻断澄清" : "资料已可用于下一阶段", [
    element("p", { text: quick ? "快速流程会自动保存叙事结构、逐页大纲并推进到样品确认。若当前尚未推进，可从此处恢复生成。" : "阻断澄清已完成。生成叙事结构后，任务会进入需要人工确认的叙事阶段。" }),
    start,
    message,
  ]);
}

function questionCard(question, answer, index) {
  const options = normalizedOptions(question.options || []);
  const answerValue = typeof answer === "string" ? answer : answer?.option === "Other" ? answer.other : answer?.option;
  const custom = Boolean(answerValue && !options.some((option) => option.value === answerValue));
  const helperId = `question-${index}-helper`;
  const errorId = `question-${index}-error`;
  const otherId = `question-${index}-other`;
  const other = element("input", {
    className: "input question-other__input",
    id: otherId,
    "data-other": "true",
    value: custom ? answerValue : "",
    placeholder: "输入更准确的答案",
    autocomplete: "off",
    onInput: (event) => {
      const card = event.currentTarget.closest("fieldset");
      if (event.currentTarget.value.trim()) card.querySelectorAll('input[type="radio"]').forEach((radio) => { radio.checked = false; });
      setQuestionError(card, "");
      card.dispatchEvent(new CustomEvent("answerchange"));
    },
  });
  const card = element("fieldset", {
    className: "question-card",
    "data-qid": question.question_id,
    "aria-describedby": `${helperId} ${errorId}`,
    tabIndex: -1,
  }, [
    element("legend", { className: "question-card__legend" }, [
      element("span", { className: "question-number", text: String(index + 1).padStart(2, "0"), "aria-hidden": "true" }),
      element("span", { text: question.prompt }),
      element("span", { className: "sr-only", text: question.blocking ? "（必答）" : "（选答）" }),
    ]),
    element("p", { className: "question-helper", id: helperId, text: question.helper_text || "请选择一项，也可以填写自己的答案。" }),
    element("div", { className: "question-options" }, options.map((option, optionIndex) => {
      const radio = element("input", {
        type: "radio",
        name: `q-${index}`,
        value: option.value,
        checked: option.value === answerValue,
        id: `question-${index}-${optionIndex}`,
        onChange: (event) => {
          if (!event.currentTarget.checked) return;
          const fieldset = event.currentTarget.closest("fieldset");
          fieldset.querySelector("[data-other]").value = "";
          setQuestionError(fieldset, "");
          fieldset.dispatchEvent(new CustomEvent("answerchange"));
        },
      });
      return element("label", { className: "question-option", htmlFor: radio.id }, [
        radio,
        element("span", { className: "question-option__copy" }, [
          element("strong", { text: option.label }),
          option.description ? element("small", { text: option.description }) : null,
        ]),
      ]);
    })),
    element("div", { className: "question-other" }, [
      element("label", { className: "field__label", htmlFor: otherId, text: "也可以输入自己的答案" }),
      other,
    ]),
    element("p", { className: "field__error question-error", id: errorId, role: "alert" }),
  ]);
  return card;
}

function normalizedOptions(options) {
  return options.map((option) => typeof option === "string"
    ? { value: option, label: option, description: "" }
    : { value: option.value, label: option.label || option.value, description: option.description || "" });
}

function readAnswer(card, question) {
  const custom = card.querySelector("[data-other]")?.value.trim();
  if (custom && question.allow_other !== false) return { option: "Other", other: custom };
  const selected = card.querySelector('input[type="radio"]:checked');
  return selected ? { option: selected.value } : null;
}

function setQuestionError(card, message) {
  const error = card.querySelector(".question-error");
  if (error) error.textContent = message;
  if (message) card.setAttribute("aria-invalid", "true");
  else card.removeAttribute("aria-invalid");
}

function clarificationRound(clarification) {
  if (clarification.question_source !== "model") return null;
  if (!Number.isInteger(clarification.round) || !Number.isInteger(clarification.max_rounds)) return null;
  return `第 ${clarification.round}/${clarification.max_rounds} 轮`;
}

function clarificationSource(clarification) {
  const modelGenerated = clarification.question_source === "model";
  const round = clarificationRound(clarification);
  return element("div", { className: "clarification-source" }, [
    badge(modelGenerated ? "AI 生成问题" : "系统补充问题", modelGenerated ? "primary" : "warning"),
    round ? badge(round, "primary") : null,
    element("p", { className: "muted", text: modelGenerated
      ? `问题已根据当前任务生成${clarification.question_model ? ` · ${clarification.question_model}` : ""}。`
      : "这些问题来自确定性缺口检查，用于补齐任务卡中的必要信息。" }),
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
  return section("授权资源清单", resourceContent(view, context), { description: "仅清单内且 hash 匹配的资源可以进入样品和全稿。" });
}

function clarificationResources(view, context) {
  const count = (view.manifest?.resources || []).length;
  return element("details", { className: "card resource-disclosure" }, [
    element("summary", {}, [
      element("span", { text: "授权资源（辅助信息）" }),
      badge(`${count} 项`),
    ]),
    element("div", { className: "resource-disclosure__body" }, resourceContent(view, context)),
  ]);
}

function resourceContent(view, context) {
  const manifest = view.manifest || {};
  const items = manifest.resources || [];
  const warnings = manifest.warnings || [];
  return [
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
  ];
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
