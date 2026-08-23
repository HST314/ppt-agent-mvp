import { api, ApiError } from "./api.js?v=2026.08.23.100340566066";
import { JobTracker } from "./job-tracker.js?v=2026.08.23.100340566066";
import { currentRoute, installRouter, navigate } from "./router.js?v=2026.08.23.100340566066";
import { applyTheme, badge, brandMark, button, element, icon, iconButton, preferredTheme, showToast } from "./shell.js?v=2026.08.23.100340566066";
import { bindJobIntent, clearIdempotencyKey, getOrCreateIdempotencyKey, storageKeyForJob, storedJobIntents } from "./store.js?v=2026.08.23.100340566066";
import { inlineError, setBusy } from "./components/index.js?v=2026.08.23.100340566066";
import { renderStage } from "./stages/index.js?v=2026.08.23.100340566066";
import { setVersionMatchGuard } from "./stages/shared.js?v=2026.08.23.100340566066";

const app = document.getElementById("app");
const APP_BUILD = document.querySelector('meta[name="app-build"]')?.content || "unknown";
const tracker = new JobTracker();
const jobTransports = new Map();
const jobSnapshots = new Map();
const jobEvents = new Map();
const jobAudits = new Map();
const authorityRefreshes = new Map();
let renderGeneration = 0;
let activeController = null;
let hasUnsavedDraft = false;
let renderedAuthority = null;
let acceptedLocation = `${window.location.pathname}${window.location.search}${window.location.hash}`;
let runtimeProbe = null;
let versionMismatchNotified = false;
let runtimeState = { browserOnline: navigator.onLine, backendReachable: null, runtimeReady: null, health: null };

const STATUS = {
  ready: ["就绪", "success"],
  running: ["运行中", "primary"],
  waiting_for_user: ["等待人工", "warning"],
  paused: ["已暂停", "warning"],
  cancelled: ["已取消", "danger"],
  failed: ["失败", "danger"],
  completed: ["已完成", "success"],
};

const OPERATION_STAGES = {
  "clarification.generate": ["clarification"],
  "narrative.generate": ["clarification", "narrative"],
  "outline.generate": ["narrative", "outline"],
  "samples.generate": ["outline", "sample"],
  "samples.modify": ["outline", "sample"],
  "deck.generate": ["deck"],
  "deck.modify": ["deck", "review"],
  "inspection.run": ["deck", "review"],
  "inspection.fix": ["review"],
  "delivery.publish": ["delivery"],
};

const JOB_ERROR_PRESENTATIONS = {
  stage_tool_contract_error: {
    label: "阶段工具契约错误",
    guidance: "模型连续请求了本阶段不允许的工具或文件。系统已停止无效调用；请重试，若再次发生请提供诊断 ID。",
  },
};

function taskModeLabel(mode, compact = false) {
  if (mode === "quick") return compact ? "快速" : "快速生成";
  if (mode === "auto") return compact ? "自动" : "自动模式";
  return compact ? "人工" : "人工模式";
}

applyTheme(preferredTheme());
installRouter(renderRoute);
window.addEventListener("beforeunload", (event) => {
  if (!hasUnsavedDraft) return;
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("online", () => {
  showToast("网络连接已恢复");
  refreshRuntimeStatus();
  if (!hasUnsavedDraft) renderRoute(currentRoute());
});
window.addEventListener("offline", () => {
  showToast("网络连接已断开，已保留当前编辑内容");
  runtimeState = { browserOnline: false, backendReachable: false, runtimeReady: false, health: null };
  updateRuntimeUI();
  if (!hasUnsavedDraft) renderRoute(currentRoute());
});
setVersionMatchGuard(ensureVersionMatchAllowed);
renderRoute(currentRoute());
refreshRuntimeStatus();
window.setInterval(() => refreshRuntimeStatus(), 15_000);
window.setInterval(refreshJobClocks, 1000);

async function renderRoute(route, authority = null) {
  const requestedLocation = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (requestedLocation !== acceptedLocation && hasUnsavedDraft && !window.confirm("当前阶段有未保存修改，确定离开吗？")) {
    window.history.pushState({}, "", acceptedLocation);
    return;
  }
  if (requestedLocation !== acceptedLocation) hasUnsavedDraft = false;
  acceptedLocation = requestedLocation;
  const generation = ++renderGeneration;
  activeController?.abort("navigation");
  activeController = new AbortController();
  tracker.stopAll();
  app.setAttribute("aria-busy", "true");
  if (route.name === "home") {
    renderedAuthority = null;
    await renderHome(generation);
  }
  else if (route.name === "components") {
    renderedAuthority = null;
    renderComponents();
  }
  else await renderWorkspace(route, generation, authority);
  if (generation === renderGeneration) app.setAttribute("aria-busy", "false");
}

function topbar(context = {}) {
  const theme = document.documentElement.dataset.theme;
  const themeButton = iconButton(theme === "dark" ? "sun" : "moon", theme === "dark" ? "切换浅色主题" : "切换深色主题", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
    const nextIcon = next === "dark" ? "sun" : "moon";
    const label = next === "dark" ? "切换浅色主题" : "切换深色主题";
    themeButton.replaceChildren(icon(nextIcon));
    themeButton.setAttribute("aria-label", label);
    themeButton.title = label;
  });
  const home = element("a", { className: "topbar__brand", href: "/", "aria-label": "返回任务首页", onClick: intercept }, [
    brandMark(),
    element("span", { className: "topbar__title", text: "PPT Agent" }),
  ]);
  const connection = runtimeStatusBadges();
  const build = badge(`Build ${APP_BUILD}`, "primary");
  build.classList.add("badge--build");
  const settingsButton = context.task ? null : iconButton("settings", "打开设置", openSettings);
  const primaryNav = context.task ? element("nav", { className: "primary-nav", "aria-label": "任务一级导航" }, [
    taskViewLink(context.task.task_id, "workspace", "工作区", context.route?.view),
    taskViewLink(context.task.task_id, "status", "状态", context.route?.view),
    taskViewLink(context.task.task_id, "settings", "设置", context.route?.view),
  ]) : null;
  const bar = element("header", { className: "topbar" }, [
    context.onMenu ? iconButton("menu", "打开任务与阶段导航", context.onMenu, "menu-button") : null,
    home,
    context.task ? element("div", { className: "topbar__context" }, [
      element("span", { className: "context-label", text: "当前任务" }),
      element("strong", { text: context.task.task_id }),
      badge(taskModeLabel(context.task.mode)),
      context.branch ? badge(`分支 ${context.branch.branch_id}`, "primary") : null,
    ]) : null,
    primaryNav,
    element("div", { className: "topbar__actions" }, [
      connection,
      build,
      context.extra || null,
      settingsButton,
      themeButton,
    ]),
  ]);
  return bar;
}

function taskViewLink(taskId, view, label, current) {
  const href = `/tasks/${encodeURIComponent(taskId)}${view === "workspace" ? "" : `?view=${view}`}`;
  return element("a", { className: "primary-nav__link", href, "aria-current": current === view ? "page" : null, onClick: intercept, text: label });
}

function runtimeStatusBadges() {
  const group = element("div", { className: "runtime-status badge--connection", "data-runtime-status": "true", role: "status", "aria-live": "polite" });
  renderRuntimeBadges(group);
  return group;
}

function runtimeVersionMismatch() {
  const backendBuild = runtimeState.health?.frontend_build;
  if (!backendBuild || backendBuild === "unknown" || !APP_BUILD || APP_BUILD === "unknown") return false;
  return backendBuild !== APP_BUILD;
}

function renderRuntimeBadges(group) {
  const browserLabel = runtimeState.browserOnline ? "浏览器在线" : "浏览器离线";
  const browserTone = runtimeState.browserOnline ? "success" : "danger";
  const backendLabel = runtimeState.backendReachable === null ? "后端检测中" : runtimeState.backendReachable ? "后端可达" : "后端不可达";
  const backendTone = runtimeState.backendReachable === null ? "warning" : runtimeState.backendReachable ? "success" : "danger";
  const versionMismatch = runtimeVersionMismatch();
  let modelLabel = "模型检测中";
  let modelTone = "warning";
  if (runtimeState.health?.startup_status === "starting") {
    modelLabel = "后台初始化中";
  } else if (runtimeState.health?.clarification_mode === "fake" && runtimeState.runtimeReady) {
    modelLabel = "模型：本地模式";
    modelTone = "primary";
  } else if (runtimeState.runtimeReady === true) {
    modelLabel = "模型可用";
    modelTone = "success";
  } else if (runtimeState.health?.model_capabilities?.status === "recovering") {
    modelLabel = "模型恢复探测中";
  } else if (runtimeState.runtimeReady === false) {
    modelLabel = "模型不可用";
    modelTone = "danger";
  }
  const signature = JSON.stringify([browserLabel, browserTone, backendLabel, backendTone, modelLabel, modelTone, versionMismatch]);
  if (group.dataset.runtimeSignature === signature) return;
  group.dataset.runtimeSignature = signature;
  const browser = badge(browserLabel, browserTone);
  const backend = badge(backendLabel, backendTone);
  const model = badge(modelLabel, modelTone);
  const code = runtimeState.health?.model_capabilities?.error?.code;
  const phase = runtimeState.health?.model_capabilities?.error?.probe_phase;
  const failedCheck = runtimeState.health?.model_capabilities?.failed_check;
  const probeId = runtimeState.health?.model_capabilities?.probe_id;
  if (code) model.title = [`运行时错误：${code}`,failedCheck ? `失败检查：${runtimeCheckLabel(failedCheck)}` : null,phase ? `失败阶段：${runtimePhaseLabel(phase)}` : null,probeId ? `探测 ID：${probeId}` : null].filter(Boolean).join(" · ");
  if (versionMismatch) {
    const warning = badge("版本不一致·需重启", "danger");
    warning.title = `前端 Build ${APP_BUILD} · 后端 Build ${runtimeState.health.frontend_build} · 后端 commit ${(runtimeState.health.backend_commit || "unknown").slice(0, 12)}`;
    group.replaceChildren(browser, backend, model, warning);
    return;
  }
  group.replaceChildren(browser, backend, model);
}

async function refreshRuntimeStatus(recheck = false) {
  if (runtimeProbe) return runtimeProbe;
  if (!navigator.onLine) {
    runtimeState = { browserOnline: false, backendReachable: false, runtimeReady: false, health: null };
    updateRuntimeUI();
    return runtimeState;
  }
  runtimeProbe = api[recheck ? "recheckRuntime" : "runtimeStatus"]().then((result) => {
    runtimeState = {
      browserOnline: navigator.onLine,
      backendReachable: result.backendReachable,
      runtimeReady: result.runtimeReady,
      health: result.ready || result.live,
    };
    updateRuntimeUI();
    return runtimeState;
  }).finally(() => { runtimeProbe = null; });
  return runtimeProbe;
}

function updateRuntimeUI() {
  document.querySelectorAll("[data-runtime-status]").forEach(renderRuntimeBadges);
  document.querySelectorAll("[data-runtime-probe-details]").forEach(renderRuntimeProbeDetails);
  document.querySelectorAll("[data-runtime-version-details]").forEach(renderRuntimeVersionDetails);
  const versionMismatch = runtimeVersionMismatch();
  document.querySelectorAll('[data-requires-runtime="true"]').forEach((control) => {
    if (runtimeState.runtimeReady && !versionMismatch) {
      if (control.dataset.runtimeDisabled === "true") {
        control.disabled = false;
        delete control.dataset.runtimeDisabled;
        control.removeAttribute("aria-description");
        control.title = "";
      }
      return;
    }
    if (!control.disabled) control.dataset.runtimeDisabled = "true";
    control.disabled = true;
    const reason = versionMismatch
      ? "前端与后端版本不一致，请先重启后端服务"
      : runtimeState.backendReachable === false
        ? "后端服务当前不可达"
        : runtimeState.health?.startup_status === "starting"
          ? "后台正在恢复任务并检测运行时，请稍后"
          : "模型运行时不可用，请先重新检测";
    control.title = reason;
    control.setAttribute("aria-description", reason);
  });
  // 版本阻断状态（data-version-disabled）独立于 disabled 当前值：mismatch 期间
  // 始终打标，动态业务闸据此保持禁用；仅记录版本门禁自己禁用的控件
  // （data-version-prev-enabled），解除时只恢复这些控件，不误改业务禁用态。
  let versionGateReleased = false;
  document.querySelectorAll('[data-requires-version-match="true"]').forEach((control) => {
    if (!versionMismatch) {
      if (control.dataset.versionDisabled === "true") {
        delete control.dataset.versionDisabled;
        versionGateReleased = true;
        if (control.dataset.versionPrevEnabled === "true") {
          delete control.dataset.versionPrevEnabled;
          control.disabled = false;
          control.removeAttribute("aria-description");
          control.title = "";
        }
      }
      return;
    }
    if (control.dataset.versionDisabled !== "true") {
      control.dataset.versionDisabled = "true";
      if (!control.disabled) control.dataset.versionPrevEnabled = "true";
    }
    control.disabled = true;
    const reason = "前端与后端版本不一致，请先重启后端服务";
    control.title = reason;
    control.setAttribute("aria-description", reason);
  });
  if (versionGateReleased) document.dispatchEvent(new CustomEvent("versiongatechange"));
  syncVersionBanner();
}

async function ensureVersionMatchAllowed() {
  await refreshRuntimeStatus();
  if (runtimeVersionMismatch()) {
    throw new ApiError("前端与后端版本不一致，请先重启后端服务再执行该操作", { code: "version_mismatch", status: 409 });
  }
}

function assertRuntimeReady() {
  if (!runtimeState.runtimeReady) {
    throw new ApiError("模型运行时不可用，请先在连接状态中重新检测", { code: "runtime_unavailable", status: 503 });
  }
}

async function ensureRuntimeActionAllowed() {
  await ensureVersionMatchAllowed();
  assertRuntimeReady();
}

function syncVersionBanner() {
  document.querySelectorAll("[data-version-mismatch-banner]").forEach((node) => node.remove());
  if (!runtimeVersionMismatch()) {
    versionMismatchNotified = false;
    return;
  }
  const shellNode = document.querySelector(".app-shell");
  if (shellNode) {
    const backendBuild = runtimeState.health?.frontend_build || "unknown";
    const commit = (runtimeState.health?.backend_commit || "unknown").slice(0, 12);
    const banner = element("div", { className: "version-banner", "data-version-mismatch-banner": "true", role: "alert" }, [
      element("strong", { text: "前端与后端版本不一致，请重启后端服务" }),
      element("span", { text: `页面资源为 Build ${APP_BUILD}，正在运行的后端为 Build ${backendBuild}（commit ${commit}）。后端进程仍在执行旧代码，模型探测与运行状态可能无效。` }),
      element("span", { className: "version-banner__steps", text: "请在运行后端的终端按 Ctrl+C 停止，重新执行 python -m uvicorn main_front:app --host 127.0.0.1 --port 8000，确认 /readyz 的 backend_commit 与 git rev-parse HEAD 相同后刷新本页面。" }),
    ]);
    const header = shellNode.querySelector(".topbar");
    if (header?.parentNode === shellNode) header.after(banner);
    else shellNode.prepend(banner);
  }
  if (!versionMismatchNotified) {
    versionMismatchNotified = true;
    showToast("前端与后端版本不一致，请重启后端服务");
  }
}

function runtimeVersionDetails() {
  const container = element("div", { "data-runtime-version-details": "true" });
  renderRuntimeVersionDetails(container);
  return container;
}

function renderRuntimeVersionDetails(container) {
  const health = runtimeState.health || {};
  const backendBuild = health.frontend_build || null;
  const commit = health.backend_commit || null;
  const config = health.config_summary_sha256 || null;
  const rows = [
    ["前端 Build", APP_BUILD],
    ["后端 Build", backendBuild || "未知"],
    ["后端 commit", commit && commit !== "unknown" ? commit : "未知"],
    ["配置摘要", config ? config.slice(0, 12) : "未知"],
  ];
  let status = "等待后端响应后校验版本";
  let mismatch = false;
  if (backendBuild) {
    mismatch = runtimeVersionMismatch();
    if (mismatch) status = "前后端版本不一致：后端进程仍在运行旧代码，请重启后端服务后刷新页面";
    else if (!commit || commit === "unknown") status = "前后端版本一致；后端 commit 未知，无法校验代码版本";
    else status = "前后端版本一致";
  }
  container.replaceChildren(
    element("dl", { className: "metadata-list", "aria-label": "版本与提交信息" }, rows.map(([key, value]) => element("div", {}, [element("dt", { text: key }), element("dd", { text: value })]))),
    element("p", { className: mismatch ? "version-warning" : "field__hint", text: status }),
  );
}

async function renderHome(generation) {
  renderLoading(false);
  try {
    const { tasks } = await api.listTasks(activeController);
    if (generation !== renderGeneration) return;
    document.title = "PPT Agent · 工作台";
    const createCard = taskForm();
    const recent = element("section", { className: "card", "aria-labelledby": "recent-heading" }, [
      element("div", { className: "card__header" }, [element("h2", { id: "recent-heading", text: "最近任务" }), badge(`${tasks.length} 个`)]),
      renderTaskList(tasks, "还没有任务。创建后会显示在这里。"),
    ]);
    const page = element("div", { className: "app-shell" }, [
      topbar({ extra: button("组件规范", { href: "/components" }) }),
      element("main", { className: "home", id: "main-content", tabIndex: -1 }, [
        element("section", { className: "home-hero", "aria-labelledby": "home-title" }, [
          element("div", {}, [
            element("p", { className: "eyebrow", text: "AI 演示文稿工作流" }),
            element("h1", { id: "home-title", text: "从任务资料到可交付演示稿，都在一个工作台。" }),
            element("p", { className: "hero-copy", text: "真实阶段、人工门禁和长任务进度始终可见。刷新或暂时离线后，也能从服务端状态继续。" }),
          ]),
          heroArt(),
        ]),
        element("div", { className: "home-grid" }, [createCard, recent]),
      ]),
    ]);
    replaceApp(page);
  } catch (error) {
    if (generation === renderGeneration) renderFatal(error, () => renderRoute(currentRoute()));
  }
}

function taskForm() {
  const taskId = element("input", { className: "input", id: "task-id", name: "task_id", required: true, pattern: "[A-Za-z0-9_\\-]+", maxLength: 128, autocomplete: "off", "aria-describedby": "task-id-hint" });
  const mode = element("select", { className: "select", id: "task-mode", name: "mode" }, [
    element("option", { value: "manual", text: "人工模式（推荐）" }),
    element("option", { value: "quick", text: "快速生成（明确页数，自动到样品）" }),
    element("option", { value: "auto", text: "兼容自动模式" }),
  ]);
  const targetSlideCount = element("input", { className: "input", id: "target-slide-count", name: "target_slide_count", type: "number", min: 1, max: 200, step: 1, inputMode: "numeric", autocomplete: "off", disabled: true, "aria-describedby": "target-slide-count-hint" });
  const targetSlideCountField = element("div", { className: "field", hidden: true }, [
    element("label", { className: "field__label", htmlFor: "target-slide-count", text: "最终页数" }),
    targetSlideCount,
    element("span", { className: "field__hint", id: "target-slide-count-hint", text: "快速生成会把此页数写入冻结任务卡，并严格约束逐页大纲。" }),
  ]);
  const updateModeFields = () => {
    const quick = mode.value === "quick";
    targetSlideCountField.hidden = !quick;
    targetSlideCount.disabled = !quick;
    targetSlideCount.required = quick;
    if (quick && !targetSlideCount.value) targetSlideCount.value = "8";
  };
  mode.addEventListener("change", updateModeFields);
  const error = element("p", { className: "field__error", id: "create-error", role: "alert" });
  const submit = button("创建任务并进入工作台", { kind: "primary", type: "submit", block: true });
  const form = element("form", { onSubmit: async (event) => {
    event.preventDefault();
    error.textContent = "";
    submit.disabled = true;
    submit.textContent = "正在创建任务…";
    try {
      const payload = { task_id: taskId.value.trim(), mode: mode.value };
      if (mode.value === "quick") payload.target_slide_count = Number(targetSlideCount.value);
      const created = await api.createTask(payload);
      showToast("任务已创建");
      navigate(`/tasks/${encodeURIComponent(created.task_id)}`);
    } catch (reason) {
      error.textContent = describeError(reason);
      taskId.setAttribute("aria-invalid", "true");
      taskId.focus();
    } finally {
      submit.disabled = false;
      submit.textContent = "创建任务并进入工作台";
    }
  } }, [
    element("div", { className: "field" }, [
      element("label", { className: "field__label", htmlFor: "task-id", text: "任务 ID" }),
      taskId,
      element("span", { className: "field__hint", id: "task-id-hint", text: "使用字母、数字、连字符或下划线，创建后不可更改。" }),
    ]),
    element("div", { className: "field" }, [element("label", { className: "field__label", htmlFor: "task-mode", text: "运行模式" }), mode]),
    targetSlideCountField,
    error,
    submit,
  ]);
  updateModeFields();
  return element("section", { className: "card card--raised", "aria-labelledby": "create-heading" }, [
    element("div", { className: "card__header" }, [element("h2", { id: "create-heading", text: "新建 PPT 任务" }), badge("第一步", "primary")]),
    form,
  ]);
}

async function renderWorkspace(route, generation, authority = null) {
  renderLoading(true);
  try {
    const [shell, recent] = await Promise.all([
      authority?.shell?.task?.task_id === route.taskId ? authority.shell : api.shell(route.taskId, activeController),
      api.listTasks(activeController),
    ]);
    if (generation !== renderGeneration) return;
    document.title = `${shell.task.task_id} · PPT Agent`;
    let sidebar;
    let scrim;
    const closeDrawer = (restoreFocus = true) => {
      sidebar.dataset.open = "false";
      scrim.dataset.open = "false";
      if (window.matchMedia("(max-width: 1023px)").matches) {
        sidebar.inert = true;
        sidebar.setAttribute("aria-hidden", "true");
      }
      if (restoreFocus) document.querySelector(".menu-button")?.focus();
    };
    const openDrawer = () => {
      sidebar.dataset.open = "true";
      scrim.dataset.open = "true";
      sidebar.inert = false;
      sidebar.setAttribute("aria-hidden", "false");
      sidebar.querySelector("button, a")?.focus();
    };
    const selected = shell.stages.find((stage) => stage.id === route.stage) || shell.stages.find((stage) => stage.status === "current") || shell.stages[shell.stages.length - 1];
    renderedAuthority = { taskId: shell.task.task_id, signature: authoritySignature(shell) };
    sidebar = workspaceSidebar(shell, recent.tasks, selected, closeDrawer);
    scrim = element("button", { className: "drawer-scrim", type: "button", "aria-label": "关闭导航", onClick: closeDrawer });
    const page = element("div", { className: "app-shell" }, [
      topbar({ task: shell.task, branch: shell.branch, route, onMenu: openDrawer }),
      element("div", { className: "shell-grid" }, [
        sidebar,
        workspaceMain(shell, selected, route),
      ]),
      scrim,
    ]);
    if (window.matchMedia("(max-width: 1023px)").matches) {
      sidebar.inert = true;
      sidebar.setAttribute("aria-hidden", "true");
    }
    const drawerMedia = window.matchMedia("(max-width: 1023px)");
    drawerMedia.addEventListener("change", (event) => {
      if (event.matches && sidebar.dataset.open !== "true") {
        sidebar.inert = true;
        sidebar.setAttribute("aria-hidden", "true");
      } else if (!event.matches) {
        sidebar.inert = false;
        sidebar.removeAttribute("aria-hidden");
      }
    }, { signal: activeController.signal });
    replaceApp(page);
    shell.active_jobs.forEach((job) => connectJob(job));
    const recentJob = route.view === "workspace" ? latestRelevantJob(shell, selected) : null;
    if (recentJob && ["succeeded", "failed", "cancelled", "interrupted"].includes(recentJob.status)) {
      hydrateJobDetails(recentJob);
    }
    await reconcileStoredIntents(shell.task.task_id, shell.active_jobs);
    if (generation !== renderGeneration) return;
    if (route.view === "status") {
      await renderStatusView(shell, route, generation);
    } else if (route.view === "settings") {
      await renderSettingsView(shell, route, generation);
    } else if (!lockedStage(selected)) {
      try {
        const prefetchedView = authority?.stageId === selected.id ? authority.stageView : null;
        const content = await renderStage(selected.id, stageContext(shell, selected, route, generation, prefetchedView));
        if (generation !== renderGeneration) return;
        enforceActiveJobState(content, shell.active_jobs);
        enforceTaskActionState(content, shell.task);
        enforceStageAccess(content, selected, shell.task);
        document.getElementById("stage-content")?.replaceChildren(content);
        updateRuntimeUI();
      } catch (error) {
        if (generation === renderGeneration && error?.name !== "AbortError") {
          document.getElementById("stage-content")?.replaceChildren(inlineError(describeError(error), error?.diagnosticId));
        }
      }
    }
  } catch (error) {
    if (generation === renderGeneration) renderFatal(error, () => renderRoute(currentRoute()), route.taskId);
  }
}

function workspaceSidebar(shell, recent, selected, closeDrawer) {
  return element("aside", { className: "sidebar", "aria-label": "任务与阶段导航", "data-open": "false", onKeydown: (event) => {
    if (event.key === "Escape") closeDrawer();
  } }, [
    iconButton("close", "关闭导航", closeDrawer, "sidebar__close"),
    element("div", { className: "sidebar__section" }, [button("新建任务", { href: "/", kind: "primary", block: true })]),
    element("section", { className: "sidebar__section", "aria-labelledby": "tasks-heading" }, [
      element("h2", { className: "sidebar__heading", id: "tasks-heading", text: "任务目录" }),
      renderTaskList(recent.slice(0, 20), "暂无其他任务", shell.task.task_id),
    ]),
  ]);
}

function workspaceMain(shell, selected, route) {
  const current = shell.stages.find((stage) => stage.status === "current");
  const locked = selected.status === "locked";
  const [statusLabel, tone] = STATUS[shell.task.status] || [shell.task.status, ""];
  const viewCopy = {
    workspace: [selected.label, locked ? selected.lock_reason : !current ? "任务已完成，所有阶段均已结束；内容为只读状态。" : selected.id === current.id ? "这是任务当前的权威阶段。" : `正在查看只读历史；当前阶段为“${current.label}”。`],
    status: ["运行状态", "查看全部 Job、领域事件与 Agent 审计历史。"],
    settings: ["任务与运行设置", "参数保存后会立即用于新 Job；当前 Job 保持启动时快照。"],
  }[route.view];
  const stageJobs = shell.active_jobs.filter((job) => OPERATION_STAGES[job.operation]?.includes(selected.id));
  const main = element("main", { className: "workspace", id: "main-content", tabIndex: -1 }, [
    element("div", { className: "workspace__inner" }, [
      element("header", { className: "workspace-header" }, [
        element("div", {}, [
          element("p", { className: "eyebrow", text: route.view === "workspace" ? `阶段 ${shell.stages.findIndex((stage) => stage.id === selected.id) + 1} / 8` : "PPT Agent 控制台" }),
          element("h1", { text: viewCopy[0] }),
          element("p", { text: viewCopy[1] }),
        ]),
        badge(statusLabel, tone),
      ]),
      route.view === "workspace" ? creationProgress(shell, selected) : null,
      route.view === "workspace" ? element("div", { id: "active-job-region", className: "workspace-jobs", "aria-live": "polite" }, stageJobs.map((job) => jobPanel(job, { detailed: false }))) : null,
      route.view === "workspace" ? latestJobFailure(shell, selected) : null,
      element("div", { id: "stage-content" }, route.view !== "workspace" ? [
        element("div", { className: "skeleton skeleton--title" }),
        element("div", { className: "card", role: "status" }, [element("span", { className: "sr-only", text: `正在加载${viewCopy[0]}` }), element("div", { className: "skeleton skeleton--line" }), element("div", { className: "skeleton skeleton--line" })]),
      ] : locked ? lockedState(selected, current) : [
        element("div", { className: "skeleton skeleton--title" }),
        element("div", { className: "card", role: "status" }, [element("span", { className: "sr-only", text: `正在加载${selected.label}` }), element("div", { className: "skeleton skeleton--line" }), element("div", { className: "skeleton skeleton--line" })]),
      ]),
    ]),
  ]);
  window.requestAnimationFrame(() => main.focus({ preventScroll: true }));
  return main;
}

function creationProgress(shell, selected) {
  const finished = shell.stages.filter((item) => ["completed", "skipped"].includes(item.status)).length;
  const current = shell.stages.find((item) => item.status === "current");
  return element("section", { className: "creation-progress card", "aria-labelledby": "creation-progress-title" }, [
    element("div", { className: "creation-progress__header" }, [
      element("div", {}, [element("p", { className: "eyebrow", text: "创作进度" }), element("h2", { id: "creation-progress-title", text: `${finished} / ${shell.stages.length} 个阶段完成` })]),
      badge(current ? `当前：${current.label}` : shell.task.status === "completed" ? "已完成" : `当前：${selected.label}`, current ? "primary" : shell.task.status === "completed" ? "success" : "primary"),
    ]),
    element("ol", { className: "progress-rail" }, shell.stages.map((stage, index) => {
      const disabled = stage.status === "locked";
      const link = element("a", {
        className: "progress-node", href: stage.href, "data-status": stage.status,
        "aria-current": stage.id === selected.id ? "step" : null,
        "aria-disabled": disabled ? "true" : null, title: stage.lock_reason,
        onClick: (event) => { if (disabled) event.preventDefault(); else intercept(event); },
      }, [
        element("span", { className: "progress-node__index", text: stage.status === "completed" ? "✓" : stage.status === "skipped" ? "—" : String(index + 1) }),
        element("span", { className: "progress-node__label", text: stage.label }),
        stage.lock_reason ? element("small", { text: stage.lock_reason.replace("前置条件：", "") }) : null,
      ]);
      const derive = ["completed", "skipped"].includes(stage.status) && Number.isInteger(stage.revision) ? button("从此派生", { kind: "ghost", onClick: () => navigate(`/tasks/${encodeURIComponent(shell.task.task_id)}?view=settings&branch_from_revision=${encodeURIComponent(stage.revision)}&branch_from_stage=${encodeURIComponent(stage.id)}`) }) : null;
      derive?.classList.add("progress-node__branch");
      return element("li", { className: "progress-node-wrap" }, [link, derive]);
    })),
  ]);
}

function lockedState(selected, current) {
  return element("section", { className: "card empty-state" }, [
    icon("file"),
    element("h2", { text: "该阶段尚未解锁" }),
    element("p", { text: selected.lock_reason }),
    button(`返回当前阶段：${current.label}`, { href: current.href, kind: "primary" }),
  ]);
}

function jobPanel(job, { detailed = false } = {}) {
  const hasProgress = typeof job.progress === "number";
  const terminal = ["succeeded", "failed", "cancelled", "interrupted"].includes(job.status);
  const historyWarning = job.event_history_warning;
  const transport = historyWarning
    ? (historyWarning.recovered ? "事件记录已自动修复" : "事件记录异常 · 业务状态仍可查询")
    : terminal ? "持久化记录已保留" : (jobTransports.get(job.job_id) || "正在连接进度通道");
  const businessStep = jobBusinessStep(job);
  const events = jobEvents.get(job.job_id) || [];
  const audits = jobAudits.get(job.job_id) || [];
  const metrics = job.metrics || [...events].reverse().find((event) => event.metrics)?.metrics || {};
  const failed = job.status === "failed" || job.status === "interrupted";
  const panel = element("section", {
    className: `job-panel ${job.status === "failed" || job.status === "interrupted" ? "job-panel--failed" : ""}`,
    id: `job-${job.job_id}`,
    "data-detailed": detailed ? "true" : "false",
    "aria-label": failed ? "最近一次后台任务失败" : terminal ? "最近一次后台任务执行详情" : "活动后台任务",
    role: failed ? "alert" : null,
  }, [
    element("div", { className: "job-panel__header" }, [
      element("div", {}, [
        element("strong", { text: operationLabel(job.operation) }),
        element("span", { className: "job-panel__section-label", text: "业务进度" }),
        element("p", { className: "job-panel__business-step", text: businessStep }),
      ]),
      badge(jobStatus(job).label, jobStatus(job).tone),
    ]),
    progress(hasProgress ? job.progress : null, hasProgress ? `${job.progress}%` : "等待下一业务检查点", businessStep),
    detailed ? element("dl", { className: "job-panel__meta" }, [
      element("div", {}, [
        element("dt", { text: job.started_at ? "已用时" : "等待时长" }),
        element("dd", {
          className: "job-panel__elapsed",
          "data-started-at": job.started_at || job.created_at,
          "data-finished-at": job.finished_at || "",
          text: formatDuration(elapsedSeconds(job.started_at || job.created_at, job.finished_at)),
        }),
      ]),
      element("div", {}, [
        element("dt", { text: "阶段时限" }),
        element("dd", {
          className: "job-panel__deadline",
          "data-deadline-at": job.deadline_at || "",
          "data-deadline-seconds": job.deadline_seconds ?? "",
          text: deadlineLabel(job.deadline_at, job.deadline_seconds),
        }),
      ]),
      element("div", {}, [
        element("dt", { text: "传输状态" }),
        element("dd", { className: "job-panel__transport", text: transport, role: "status", "aria-live": "polite" }),
      ]),
      metricItem("Agent 步数", budgetLabel(metrics.agent_step, metrics.max_steps)),
      metricItem("模型请求", budgetLabel(metrics.provider_calls, metrics.max_provider_calls)),
      metricItem("Skill 工具调用", budgetLabel(metrics.tool_calls, metrics.max_tool_calls)),
    ]) : element("p", { className: "field__hint", text: `${job.branch_id ? `分支 ${job.branch_id} · ` : ""}${job.started_at ? `已运行 ${formatDuration(elapsedSeconds(job.started_at, job.finished_at))}` : "等待执行资源"}` }),
    job.status === "cancellation_requested" || job.cancellation_requested ? element("p", {
      className: "job-panel__cancel-feedback",
      role: "status",
      text: "取消请求已送达；正在等待当前安全停止点，期间不会提交新的业务结果。",
    }) : null,
    historyWarning ? element("p", {
      className: "job-panel__storage-warning",
      role: historyWarning.recovered ? "status" : "alert",
      text: historyWarning.recovered
        ? `${historyWarning.message}；原始记录已备份。`
        : `${historyWarning.message}；系统会自动重试，业务任务无需重新提交。`,
    }) : null,
    element("p", { className: "field__hint", text: detailed ? "执行历史已持久化；可以安全离开此页。" : "可以安全离开此页；详细历史请在“状态”中查看。" }),
    job.operation !== "clarification.generate" && !["succeeded", "failed", "cancelled", "interrupted"].includes(job.status) ? button(job.cancellation_requested ? "已请求取消" : "取消后台任务", { kind: "ghost", disabled: job.cancellation_requested, onClick: async () => {
      try {
        const next = await api.cancelJob(job.job_id);
        updateJobPanel(next);
      } catch (error) {
        showToast(describeError(error));
      }
    } }) : null,
    job.error ? inlineError(job.error.message || "后台任务失败", job.error.diagnostic_id) : null,
    job.error?.agent_audit_id ? element("p", { className: "field__hint", text: `Agent 审计 ID：${job.error.agent_audit_id}` }) : null,
    detailed ? executionDetails(job, events, audits) : null,
  ]);
  if (failed) {
    const presentation = JOB_ERROR_PRESENTATIONS[job.error?.code];
    panel.append(
      presentation ? element("p", { className: "field__hint", text: `失败类型：${presentation.label}` }) : "",
      presentation ? element("p", { text: presentation.guidance }) : "",
      job.error?.code ? element("p", { className: "field__hint", text: `错误代码：${job.error.code}` }) : "",
      button("前往本阶段操作区重试", { kind: "secondary", onClick: () => {
        const control = document.querySelector('#stage-content [data-requires-runtime="true"]');
        control?.scrollIntoView({ behavior: "smooth", block: "center" });
        control?.focus({ preventScroll: true });
      } }),
    );
  }
  return panel;
}

function metricItem(label, value) {
  return element("div", {}, [element("dt", { text: label }), element("dd", { text: value })]);
}

function budgetLabel(value, limit) {
  return Number.isInteger(value) && Number.isInteger(limit) ? `${value} / ${limit}` : "等待 Agent 上报";
}

function executionDetails(job, events, audits) {
  const failed = ["failed", "interrupted"].includes(job.status);
  const details = element("details", { className: "job-execution", open: failed && events.length > 0 }, [
    element("summary", { text: `执行详情 · ${events.filter((event) => event.type !== "heartbeat").length} 条事件` }),
    events.length ? element("ol", { className: "job-timeline", "aria-label": "后台执行事件时间线" }, events
      .filter((event) => event.type !== "heartbeat")
      .map((event) => element("li", { className: `job-timeline__item job-timeline__item--${eventTone(event)}` }, [
        element("span", { className: "job-timeline__marker", "aria-hidden": "true" }),
        element("div", {}, [
          element("strong", { text: eventLabel(event) }),
          element("p", { text: event.message && ["failed", "interrupted"].includes(event.type)
            ? `错误：${event.message}`
            : event.message || jobBusinessStep({ ...job, current_step: event.step }) }),
          element("small", { text: `${formatClock(event.at)} · 序号 ${event.seq}` }),
        ]),
      ]))) : element("p", { className: "muted", text: "正在恢复持久化事件记录…" }),
    audits.length ? element("details", { className: "job-audit" }, [
      element("summary", { text: `技术审计 · ${audits.length} 次 Agent 运行` }),
      element("pre", { className: "code-block job-audit__json", text: JSON.stringify(audits, null, 2), tabIndex: 0 }),
    ]) : null,
  ]);
  return details;
}

function eventLabel(event) {
  const labels = {
    queued: "进入队列", started: "开始执行", checkpoint: stepLabel(event.step),
    succeeded: "执行完成", failed: "执行失败", cancelled: "已取消", interrupted: "执行中断",
  };
  return labels[event.type] || event.type;
}

function stepLabel(step) {
  return {
    provider_request: "模型请求已发送",
    provider_response: "模型响应已返回",
    waiting_model: "等待模型响应",
    skill_loading: "Skill 工具调用开始",
    skill_completed: "Skill 工具调用完成",
    validating_output: "校验结构化输出",
    validating_html: "校验 HTML",
    saving_result: "保存业务结果",
    generating_batch: "生成页面批次",
    agent_completed: "Agent 阶段完成",
    completed: "业务操作完成",
  }[step] || "执行检查点";
}

function eventTone(event) {
  if (["failed", "interrupted"].includes(event.type) || event.metrics?.tool_failed) return "danger";
  if (event.type === "succeeded") return "success";
  return "primary";
}

function latestJobFailure(shell, selected) {
  const latest = latestRelevantJob(shell, selected);
  if (!latest || !["succeeded", "failed", "cancelled", "interrupted"].includes(latest.status)) return null;
  return jobPanel(latest, { detailed: false });
}

function latestRelevantJob(shell, selected) {
  return (shell.latest_jobs || [])
    .filter((job) => OPERATION_STAGES[job.operation]?.includes(selected.id))
    .sort((left, right) => `${left.created_at}:${left.job_id}`.localeCompare(`${right.created_at}:${right.job_id}`))
    .at(-1);
}

async function hydrateJobDetails(job) {
  jobSnapshots.set(job.job_id, job);
  try {
    const [history, auditResponse] = await Promise.all([
      api.jobEventHistory(job.job_id),
      api.jobAgentAudits(job.job_id),
    ]);
    jobEvents.set(job.job_id, dedupeEvents(history.events));
    jobAudits.set(job.job_id, auditResponse.audits || []);
    updateJobPanel(job);
  } catch (_error) {
    // The summary remains usable when optional execution details cannot load.
  }
}

function dedupeEvents(events) {
  const seen = new Set();
  return (events || []).filter((event) => {
    const key = `${event.job_id}:${event.seq}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).sort((left, right) => left.seq - right.seq);
}

function rememberJobEvent(event) {
  const current = jobEvents.get(event.job_id) || [];
  jobEvents.set(event.job_id, dedupeEvents([...current, event]));
}

function authoritySignature(shell) {
  const task = shell?.task || {};
  const artifacts = Object.entries(shell?.summary?.latest_artifacts || {}).sort(([left], [right]) => left.localeCompare(right));
  return JSON.stringify({
    task: [task.stage, task.status, task.revision, task.waiting_reason, task.required_action],
    artifacts,
  });
}

function selectedStage(shell, route) {
  return shell.stages.find((stage) => stage.id === route.stage)
    || shell.stages.find((stage) => stage.status === "current")
    || shell.stages[shell.stages.length - 1];
}

async function readAuthoritativeStageView(taskId, stageId) {
  if (["created", "clarification"].includes(stageId)) return api.input(taskId);
  if (["narrative", "outline"].includes(stageId)) return api.planning(taskId);
  if (stageId === "sample") return api.samples(taskId);
  if (stageId === "deck") return api.deck(taskId);
  if (stageId === "review") return api.inspection(taskId);
  if (stageId === "delivery") return api.delivery(taskId);
  return null;
}

async function readAuthoritativeWorkspace(job) {
  const shell = await api.shell(job.task_id);
  const route = currentRoute();
  if (route.name !== "workspace" || route.taskId !== job.task_id || route.view !== "workspace") {
    return { shell, route, stageId: null, stageView: null };
  }
  const stage = selectedStage(shell, route);
  const stageView = stage && !lockedStage(stage)
    ? await readAuthoritativeStageView(job.task_id, stage.id)
    : null;
  return { shell, route, stageId: stage?.id || null, stageView };
}

function authoritativeReferenceReached(job, authority) {
  const expected = job.result;
  if (!expected) return true;
  const shell = authority?.shell;
  if (!shell || shell.task?.task_id !== expected.task_id) return false;
  const expectedRevision = Number.isInteger(expected.revision) ? expected.revision : null;
  const shellRevision = Number.isInteger(shell.task?.revision) ? shell.task.revision : null;
  if (expectedRevision !== null && (shellRevision === null || shellRevision < expectedRevision)) return false;
  const viewRevision = authority?.stageView?.state?.revision;
  if (expectedRevision !== null && authority?.stageId && (!Number.isInteger(viewRevision) || viewRevision < expectedRevision)) return false;
  if (expectedRevision !== null && shellRevision > expectedRevision) return true;
  if (expected.stage && shell.task?.stage !== expected.stage) return false;
  if (expected.status && shell.task?.status !== expected.status) return false;
  const actualArtifacts = shell.summary?.latest_artifacts || {};
  return Object.entries(expected.artifacts || {}).every(([kind, reference]) => !reference || actualArtifacts[kind] === reference);
}

function scheduleCheckpointAuthorityRefresh(job) {
  if (authorityRefreshes.has(job.job_id)) return authorityRefreshes.get(job.job_id);
  const refresh = readAuthoritativeWorkspace(job).then(async (authority) => {
    const route = currentRoute();
    if (route.name !== "workspace" || route.taskId !== job.task_id) return authority;
    const changed = renderedAuthority?.taskId !== job.task_id
      || renderedAuthority.signature !== authoritySignature(authority.shell);
    if (changed && !hasUnsavedDraft) {
      const stage = route.view === "workspace" ? selectedStage(authority.shell, route) : null;
      const currentAuthority = stage?.id === authority.stageId
        ? { ...authority, route, stageId: stage.id }
        : { ...authority, route, stageId: null, stageView: null };
      await renderRoute(route, currentAuthority);
    }
    return authority;
  }).catch(() => null).finally(() => authorityRefreshes.delete(job.job_id));
  authorityRefreshes.set(job.job_id, refresh);
  return refresh;
}

function connectJob(job, storageKey = null) {
  const recoveredStorageKey = storageKey || storageKeyForJob(job.job_id);
  jobSnapshots.set(job.job_id, job);
  tracker.track(job, {
    onUpdate: (next) => {
      jobSnapshots.set(next.job_id, next);
      updateJobPanel(next);
    },
    onEvent: (event) => {
      rememberJobEvent(event);
      const next = updateJobEvent(event);
      if (event.type === "checkpoint" && next) scheduleCheckpointAuthorityRefresh(next);
    },
    onTransport: (state, details) => updateJobTransport(job.job_id, state, details),
    onTerminalReconcile: async (finished) => {
      const authority = await readAuthoritativeWorkspace(finished);
      return { ready: finished.status !== "succeeded" || authoritativeReferenceReached(finished, authority), authority };
    },
    onComplete: async (finished, completion) => {
      jobSnapshots.set(finished.job_id, finished);
      jobTransports.delete(finished.job_id);
      if (recoveredStorageKey) clearIdempotencyKey(recoveredStorageKey, finished.job_id);
      const label = operationLabel(finished.operation);
      if (finished.status === "succeeded") showToast(`${label}已完成`);
      else if (finished.status === "failed") showToast(`${label}失败：${finished.error?.message || "请查看阶段内详情"}`);
      else showToast(`${label}已${finished.status === "cancelled" ? "取消" : "中断"}，请查看阶段内详情`);
      await hydrateJobDetails(finished);
      let authority = completion?.reconcileValue?.authority || null;
      try {
        // The user may confirm or otherwise advance the task while optional Job
        // details are loading. Re-read here so a terminal snapshot can never
        // overwrite a newer business revision with its older Shell.
        authority = await readAuthoritativeWorkspace(finished);
      } catch (_error) {
        // The successfully reconciled snapshot remains a safe fallback; the
        // route renderer will perform its normal API reads when it is absent.
      }
      const route = currentRoute();
      if (route.name === "workspace" && route.taskId === finished.task_id) await renderRoute(route, authority);
    },
  });
}

async function reconcileStoredIntents(taskId, activeJobs) {
  const activeIds = new Set(activeJobs.map((job) => job.job_id));
  const stored = storedJobIntents(taskId).filter((item) => !activeIds.has(item.jobId));
  await Promise.all(stored.map(async ({ jobId, storageKey }) => {
    try {
      const job = await api.getJob(jobId);
      if (["succeeded", "failed", "cancelled", "interrupted"].includes(job.status)) {
        // A terminal Job commits business authority before its terminal record.
        // The Shell fetched for this refresh is therefore already current.
        // Clear the stale intent synchronously instead of reconnecting a
        // completed tracker whose delayed onComplete render could erase a new
        // prompt the user has started typing.
        jobSnapshots.set(job.job_id, job);
        clearIdempotencyKey(storageKey, jobId);
        return;
      }
      connectJob(job, storageKey);
    } catch (_error) {
      clearIdempotencyKey(storageKey, jobId);
    }
  }));
}

function stageContext(shell, selected, route, generation, authoritativeView = null) {
  return {
    taskId: shell.task.task_id,
    shell,
    selected,
    authoritativeView,
    controller: activeController,
    assertCurrent() {
      if (generation !== renderGeneration) throw new DOMException("stale view", "AbortError");
    },
    setDirty(value) { hasUnsavedDraft = Boolean(value); },
    refresh: () => renderRoute(currentRoute()),
    goTo: (stage) => navigate(`/tasks/${encodeURIComponent(shell.task.task_id)}${stage ? `?stage=${encodeURIComponent(stage)}` : ""}`),
    startJob: (operation, payload, options = {}) => startJob(shell.task.task_id, operation, payload, route, options),
    retryClarification: (options = {}) => retryClarification(shell.task.task_id, route, options),
  };
}

async function startJob(taskId, operation, payload, route, { buttonNode = null, region = null } = {}) {
  const intent = getOrCreateIdempotencyKey(taskId, operation, payload);
  return startTrackedJob({
    taskId,
    route,
    buttonNode,
    region,
    intent,
    requiresRuntime: operation !== "delivery.publish",
    create: () => api.createJob(taskId, { operation, payload, idempotency_key: intent.value }),
  });
}

async function retryClarification(taskId, route, { buttonNode = null, region = null } = {}) {
  const intent = getOrCreateIdempotencyKey(taskId, "clarification.generate", {});
  const job = await startTrackedJob({
    taskId,
    route,
    buttonNode,
    region,
    intent,
    busyLabel: "正在创建重试任务…",
    create: () => api.retryClarification(taskId, intent.value),
  });
  if (job && !["succeeded", "failed", "cancelled", "interrupted"].includes(job.status)) renderRoute(currentRoute());
  return job;
}

async function startTrackedJob({ taskId, route, buttonNode, region, intent, create, requiresRuntime = true, busyLabel = "正在创建后台任务…" }) {
  region?.replaceChildren();
  setBusy(buttonNode, true, busyLabel);
  try {
    // 版本门禁无条件先行：包括 delivery.publish 在内的所有 Job 创建路径，
    // 在派发前都必须通过前后端版本一致性校验；模型就绪校验仅限运行时操作。
    await ensureVersionMatchAllowed();
    if (requiresRuntime) assertRuntimeReady();
    const job = await create();
    bindJobIntent(job, intent.storageKey);
    let activeRegion = document.getElementById("active-job-region");
    if (!activeRegion) activeRegion = region;
    const existing = document.getElementById(`job-${job.job_id}`);
    if (existing) existing.replaceWith(jobPanel(job));
    else activeRegion?.append(jobPanel(job));
    connectJob(job, intent.storageKey);
    if (buttonNode) {
      buttonNode.textContent = "后台任务运行中";
      buttonNode.disabled = true;
    }
    return job;
  } catch (error) {
    if (region) region.replaceChildren(inlineError(describeError(error), error?.diagnosticId));
    setBusy(buttonNode, false);
    updateRuntimeUI();
    return null;
  }
}

function lockedStage(stage) {
  return stage.status === "locked";
}

function enforceActiveJobState(content, activeJobs) {
  const active = activeJobs?.[0];
  if (!active) return;
  const reason = `${operationLabel(active.operation)}正在运行；完成后可继续操作`;
  content.querySelectorAll('[data-mutates="true"]').forEach((control) => {
    control.disabled = true;
    control.title = reason;
    control.setAttribute("aria-description", reason);
  });
}

function enforceTaskActionState(content, task) {
  if (!["paused", "cancelled", "failed", "completed"].includes(task.status)) return;
  content.querySelectorAll('[data-mutates="true"]').forEach((control) => {
    if (task.status === "completed" && control.dataset.allowCompleted === "true") return;
    control.disabled = true;
    control.title = `任务状态 ${task.status} 不允许执行该动作`;
    control.setAttribute("aria-description", control.title);
  });
}

function enforceStageAccess(content, stage, task) {
  if (!["completed", "skipped"].includes(stage.status)) return;
  content.querySelectorAll('[data-mutates="true"]').forEach((control) => {
    // 已完成任务的交付页是终态而非历史：显式允许完成后动作（如交付派生）保持可用。
    if (task?.status === "completed" && control.dataset.allowCompleted === "true") return;
    control.disabled = true;
    control.title = "历史阶段为只读；如需继续编辑，请从该节点创建分支";
    control.setAttribute("aria-description", control.title);
  });
}

function updateJobPanel(job) {
  const old = document.getElementById(`job-${job.job_id}`);
  if (!old) return;
  const executionWasOpen = old.querySelector(":scope > .job-execution")?.open;
  const auditWasOpen = old.querySelector(".job-audit")?.open;
  const next = jobPanel(job, { detailed: old.dataset.detailed === "true" });
  if (executionWasOpen) next.querySelector(":scope > .job-execution")?.setAttribute("open", "");
  if (auditWasOpen) next.querySelector(".job-audit")?.setAttribute("open", "");
  old.replaceWith(next);
}

function updateJobEvent(event) {
  if (event.type === "heartbeat") return;
  const current = jobSnapshots.get(event.job_id);
  if (!current) return;
  const next = {
    ...current,
    current_step: event.step || current.current_step,
    progress: typeof event.progress === "number" ? event.progress : current.progress,
    metrics: event.metrics || current.metrics,
    last_seq: Math.max(current.last_seq || 0, event.seq),
  };
  jobSnapshots.set(event.job_id, next);
  updateJobPanel(next);
  return next;
}

function updateJobTransport(jobId, state, details = {}) {
  const labels = {
    sse: details.recovered ? "进度通道已恢复" : "进度通道已连接",
    heartbeat: `进度通道正常${details.at ? ` · ${formatClock(details.at)}` : ""}`,
    "sse-retry": "进度通道重连中",
    polling: "进度通道重连中 · 状态轮询可用",
    "sse-recovery": "正在恢复实时进度通道",
    "terminal-reconcile": "正在核对权威业务视图",
    "terminal-reconcile-exhausted": "权威业务视图同步超时 · 已显示最新可用状态",
    "storage-error": `事件存储暂不可用 · ${Math.max(1, Math.ceil((details.delay || 1000) / 1000))} 秒后重试`,
  };
  const label = labels[state] || "正在确认进度通道";
  jobTransports.set(jobId, label);
  const output = document.getElementById(`job-${jobId}`)?.querySelector(".job-panel__transport");
  if (output) output.textContent = label;
}

function refreshJobClocks() {
  document.querySelectorAll(".job-panel__elapsed").forEach((output) => {
    output.textContent = formatDuration(elapsedSeconds(output.dataset.startedAt, output.dataset.finishedAt));
  });
  document.querySelectorAll(".job-panel__deadline").forEach((output) => {
    output.textContent = deadlineLabel(output.dataset.deadlineAt, Number(output.dataset.deadlineSeconds) || null);
  });
}

function elapsedSeconds(value, finishedValue = "") {
  const started = Date.parse(value || "");
  const finished = Date.parse(finishedValue || "");
  const end = Number.isFinite(finished) ? finished : Date.now();
  return Number.isFinite(started) ? Math.max(0, Math.floor((end - started) / 1000)) : 0;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours ? `${hours}时 ${minutes}分 ${remainder}秒` : minutes ? `${minutes}分 ${remainder}秒` : `${remainder}秒`;
}

function deadlineLabel(deadlineAt, deadlineSeconds) {
  const deadline = Date.parse(deadlineAt || "");
  if (!Number.isFinite(deadline)) return deadlineSeconds ? `启动后最长 ${formatDuration(deadlineSeconds)}` : "等待后端下发";
  const remaining = Math.ceil((deadline - Date.now()) / 1000);
  if (remaining <= 0) return `硬截止 ${formatClock(deadlineAt)} · 已到达，等待任务结束`;
  return `硬截止 ${formatClock(deadlineAt)} · 剩余 ${formatDuration(remaining)}`;
}

function formatClock(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}

function jobStatus(job) {
  if (job.status === "queued") return { label: "排队中", tone: "warning" };
  if (job.status === "cancellation_requested" || job.cancellation_requested) return { label: "正在取消", tone: "warning" };
  if (job.status === "failed") return { label: "失败", tone: "danger" };
  if (job.status === "cancelled") return { label: "已取消", tone: "danger" };
  if (job.status === "succeeded") return { label: "已完成", tone: "success" };
  return { label: "执行中", tone: "primary" };
}

function jobBusinessStep(job) {
  if (job.status === "queued") return "等待执行资源";
  if (job.status === "cancellation_requested" || job.cancellation_requested) return "正在取消：等待当前安全停止点";
  const terminal = {
    succeeded: "业务操作已完成",
    failed: "业务操作失败",
    cancelled: "业务操作已取消，未提交新结果",
    interrupted: "业务操作被中断",
  };
  if (terminal[job.status]) return terminal[job.status];
  const operations = {
    "clarification.generate": "AI 正在阅读任务卡并生成澄清问题",
    "narrative.generate": "等待模型生成叙事结构",
    "outline.generate": "等待模型生成并校验逐页大纲",
    "samples.generate": "等待模型生成、校验并保存 HTML 样品",
    "samples.modify": "等待模型修改、校验并保存 HTML 样品",
    "deck.generate": "等待模型生成、校验并保存完整演示稿",
    "deck.modify": "等待模型修改、校验并保存完整演示稿",
    "inspection.run": "正在执行质量检查并保存报告",
  };
  const steps = {
    waiting_model: "等待模型响应",
    provider_request: "模型请求已发送，等待响应",
    provider_response: "模型响应已返回，正在处理",
    skill_loading: "正在读取 Skill 与任务资料",
    skill_completed: "Skill 工具调用已完成",
    validating_output: "正在校验模型输出与阶段 Schema",
    validating_html: "正在校验 HTML 与页面结构",
    technical_correction: "模型输出未通过技术校验，正在自动修正",
    safe_fallback: "模型输出仍不合规，正在生成安全样品",
    saving_result: "正在保存版本与业务状态",
    generating_batch: "正在生成页面批次",
    agent_completed: "模型输出已返回，正在执行技术校验",
  };
  if (steps[job.current_step]) return steps[job.current_step];
  return operations[job.operation] || "业务操作执行中";
}

async function renderStatusView(shell, route, generation) {
  const region=document.getElementById("stage-content");
  try {
    const [jobResponse,eventResponse,auditResponse]=await Promise.all([
      api.jobs(shell.task.task_id,{limit:25}),
      api.taskEvents(shell.task.task_id,{limit:100}),
      api.taskAgentAudits(shell.task.task_id),
    ]);
    if (generation!==renderGeneration) return;
    const status=element("select",{className:"select",id:"job-status-filter"},[
      element("option",{value:"",text:"全部状态"}),
      ...["active","succeeded","failed","cancelled","interrupted"].map((value)=>element("option",{value,text:({active:"活动中",succeeded:"已完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"})[value]})),
    ]);
    const operation=element("select",{className:"select",id:"job-operation-filter"},[
      element("option",{value:"",text:"全部操作"}),
      ...Object.keys(OPERATION_STAGES).map((value)=>element("option",{value,text:operationLabel(value)})),
    ]);
    const list=element("div",{className:"status-job-list","aria-live":"polite"});
    let cursor=jobResponse.next_cursor;
    const appendJobs=(jobs,append=false)=>{
      const panels=jobs.map((job)=>jobPanel(job,{detailed:true}));
      if (append) list.append(...panels); else list.replaceChildren(...(panels.length ? panels : [element("div",{className:"empty-state empty-state--compact"},element("p",{text:"没有符合条件的 Job。"}))]));
      jobs.forEach(hydrateJobDetails);
    };
    appendJobs(jobResponse.jobs);
    const loadMore=button("加载更早记录",{kind:"secondary",disabled:!cursor,reason:"已显示全部记录",onClick:async(event)=>{
      const control=event.currentTarget; setBusy(control,true,"正在加载…");
      try {
        const response=await api.jobs(shell.task.task_id,{status:status.value,operation:operation.value,limit:25,before:cursor});
        appendJobs(response.jobs,true); cursor=response.next_cursor; control.disabled=!cursor; control.textContent=cursor ? "加载更早记录" : "已显示全部记录";
      } catch (error) { showToast(describeError(error)); setBusy(control,false); }
    }});
    const applyFilters=async()=>{
      const response=await api.jobs(shell.task.task_id,{status:status.value,operation:operation.value,limit:25});
      cursor=response.next_cursor; appendJobs(response.jobs); loadMore.disabled=!cursor; loadMore.textContent=cursor ? "加载更早记录" : "已显示全部记录";
    };
    status.addEventListener("change",()=>applyFilters().catch((error)=>showToast(describeError(error))));
    operation.addEventListener("change",()=>applyFilters().catch((error)=>showToast(describeError(error))));
    const events=eventResponse.events || [];
    const audits=auditResponse.audits || [];
    region.replaceChildren(element("div",{className:"status-layout"},[
      element("section",{className:"card status-section"},[
        element("div",{className:"card__header"},[element("div",{},[element("h2",{text:"Job 历史"}),element("p",{className:"muted",text:"按服务端分页读取；展开单项可查看完整事件与 Agent 审计。"})]),badge(`${jobResponse.jobs.length}${cursor ? "+" : ""} 条`)]),
        element("div",{className:"status-filters"},[status,operation]),list,loadMore,
      ]),
      element("section",{className:"card status-section"},[
        element("div",{className:"card__header"},[element("div",{},[element("h2",{text:"领域事件"}),element("p",{className:"muted",text:`当前分支 ${shell.branch.branch_id} 的权威状态变化。`})]),badge(`${events.length}${eventResponse.next_revision!==null ? "+" : ""} 条`)]),
        events.length ? element("ol",{className:"domain-timeline"},events.map((item)=>element("li",{},[
          element("span",{className:"domain-timeline__marker","aria-hidden":"true"}),
          element("div",{},[element("strong",{text:domainActionLabel(item.action)}),element("p",{text:`${stageLabel(item.from?.stage)} → ${stageLabel(item.to?.stage)} · 修订 ${item.to?.revision ?? "—"}`}),element("small",{text:formatClock(item.at)})]),
        ]))) : element("p",{className:"muted",text:"尚无领域事件。"}),
      ]),
      element("section",{className:"card status-section"},[
        element("div",{className:"card__header"},[element("div",{},[element("h2",{text:"Agent 审计"}),element("p",{className:"muted",text:"历史持续保留；技术详情按运行折叠展示。"})]),badge(`${audits.length} 次`)]),
        audits.length ? element("div",{className:"audit-list"},audits.slice().reverse().map((item)=>element("details",{className:"audit-item"},[element("summary",{text:`${stageLabel(item.stage)} · ${item.model || "unknown"} · ${item.audit_id || "审计记录"}`}),element("pre",{className:"code-block job-audit__json",text:JSON.stringify(item,null,2),tabIndex:0})]))) : element("p",{className:"muted",text:"尚无 Agent 审计。"}),
      ]),
    ]));
  } catch (error) {
    if (generation===renderGeneration) region?.replaceChildren(inlineError(describeError(error),error?.diagnosticId));
  }
}

function domainActionLabel(action) {
  return ({import_input:"导入任务资料",rebuild_input:"重建任务资料",confirm_outline:"确认逐页大纲",confirm_sample_version:"确认样品并进入全稿",deck_generate:"生成全稿",deck_modify:"修改全稿",inspection_complete:"完成质量检查",finalize_deck:"确定终稿",confirm_delivery:"完成交付"})[action] || action || "状态更新";
}

async function renderSettingsView(shell, route, generation) {
  const region=document.getElementById("stage-content");
  try {
    const [settings,branches]=await Promise.all([api.settings(activeController),api.branches(shell.task.task_id,activeController)]);
    if (generation!==renderGeneration) return;
    const tabs=element("div",{className:"settings-tabs",role:"tablist","aria-label":"设置分组"});
    const panels=element("div",{className:"settings-panels"});
    const groups=[];
    const register=(id,label,panel)=>{
      const tab=button(label,{kind:"ghost",onClick:()=>selectSetting(id)}); tab.id=`settings-tab-${id}`; tab.setAttribute("role","tab"); tab.setAttribute("aria-controls",`settings-panel-${id}`);
      panel.id=`settings-panel-${id}`; panel.setAttribute("role","tabpanel"); panel.setAttribute("aria-labelledby",tab.id); groups.push({id,tab,panel}); tabs.append(tab); panels.append(panel);
    };
    const selectSetting=(id)=>groups.forEach((item)=>{ const active=item.id===id; item.tab.setAttribute("aria-selected",String(active)); item.tab.tabIndex=active ? 0 : -1; item.panel.hidden=!active; });
    register("workflow","工作流",settingsGroup("澄清与工作流",settings,"workflow"));
    register("generation","生成与全稿",settingsGroup("生成 Job",settings,"jobs"));
    register("review","自检与交付",settingsGroup("自检默认值",settings,"review"));
    register("models","模型",modelSettings(settings.models));
    register("branches","分支",branchSettings(shell,branches,route));
    register("system","系统与显示",systemSettings());
    selectSetting("workflow");
    region.replaceChildren(element("div",{className:"settings-layout"},[tabs,panels]));
  } catch (error) {
    if (generation===renderGeneration) region?.replaceChildren(inlineError(describeError(error),error?.diagnosticId));
  }
}

function settingsGroup(title,settings,group) {
  const schema=settings.schema[group]; const values=settings.values[group]; const controls={}; const message=element("div",{className:"stage-message",role:"status","aria-live":"polite"});
  const fields=Object.entries(schema).map(([key,definition])=>{
    let control;
    if (definition.type==="select") control=element("select",{className:"select",id:`setting-${group}-${key}`},definition.options.map((value)=>element("option",{value,text:value,selected:value===values[key]})));
    else control=element("input",{className:"input",id:`setting-${group}-${key}`,type:"number",min:definition.minimum,max:definition.maximum,value:values[key]});
    controls[key]=control;
    return element("div",{className:"field"},[element("label",{className:"field__label",htmlFor:control.id,text:definition.label}),control,definition.minimum!==undefined ? element("span",{className:"field__hint",text:`允许范围：${definition.minimum}–${definition.maximum}`}) : null]);
  });
  const save=button("保存并立即生效",{kind:"primary",mutates:true,onClick:async()=>{
    const payload={}; payload[group]=Object.fromEntries(Object.entries(controls).map(([key,control])=>[key,schema[key].type==="integer" ? Number(control.value) : control.value]));
    setBusy(save,true,"正在保存…"); message.replaceChildren();
    try { await api.updateSettings(payload); message.append(element("p",{className:"success-message",text:"全局设置已保存；新 Job 将立即采用。"})); }
    catch (error) { message.replaceChildren(inlineError(describeError(error),error?.diagnosticId)); }
    finally { setBusy(save,false); }
  }});
  return element("section",{className:"card settings-panel"},[element("div",{className:"card__header"},[element("div",{},[element("h2",{text:title}),element("p",{className:"muted",text:"保存会原子更新应用级全局 YAML；当前运行不会被中途改写。"})])]),...fields,save,message]);
}

function modelSettings(models) {
  const gateways=models.gateways || [];
  return element("section",{className:"card settings-panel"},[
    element("div",{className:"card__header"},[element("div",{},[element("h2",{text:"模型与凭证状态"}),element("p",{className:"muted",text:"仅显示环境变量名称和安全摘要，不回显密钥。"})]),badge(models.mode==="agent" ? "Agent 模式" : "本地模式","primary")]),
    gateways.length ? element("div",{className:"model-list"},gateways.map((item)=>element("article",{className:"notice"},[element("strong",{text:item.model}),element("p",{text:`${item.type} · Key ${item.config?.api_key_env || "—"} · Base URL ${item.config?.base_url_env || "—"}`} )]))) : element("p",{className:"muted",text:"当前使用本地确定性模式，无外部模型凭证。"}),
    runtimeStatusBadges(),runtimeProbeDetails(),
  ]);
}

function branchSettings(shell,branches,route) {
  const name=element("input",{className:"input",id:"new-branch-name",placeholder:"例如 refine-layout",pattern:"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"});
  const create=button("从当前节点创建并切换",{kind:"primary",mutates:true}); const message=element("div",{className:"stage-message",role:"status","aria-live":"polite"});
  const sourceRevision=route.sourceRevision ?? shell.task.revision;
  const sourceStage=route.sourceStage ? stageLabel(route.sourceStage) : stageLabel(shell.task.stage);
  create.addEventListener("click",async()=>{
    setBusy(create,true,"正在创建…");
    try { await api.createBranch(shell.task.task_id,{branch_id:name.value.trim(),source_branch:branches.active,source_revision:sourceRevision,switch:true}); showToast("分支已创建并切换"); navigate(`/tasks/${encodeURIComponent(shell.task.task_id)}`); }
    catch (error) { message.replaceChildren(inlineError(describeError(error),error?.diagnosticId)); setBusy(create,false); }
  });
  return element("section",{className:"card settings-panel"},[
    element("div",{className:"card__header"},[element("div",{},[element("h2",{text:"创作分支"}),element("p",{className:"muted",text:"历史节点只读；继续创作会派生分支，并复用不可变版本与资源。"})]),badge(`当前 ${branches.active}`,"primary")]),
    element("div",{className:"branch-create"},[element("div",{className:"field"},[element("label",{className:"field__label",htmlFor:name.id,text:"新分支名称"}),name,element("span",{className:"field__hint",text:`来源：${branches.active} · ${sourceStage} · 修订 ${sourceRevision}`})]),create]),message,
    element("ul",{className:"branch-list"},branches.branches.map((item)=>element("li",{},[element("div",{},[element("strong",{text:item.branch_id}),element("small",{text:`${stageLabel(item.stage)} · 修订 ${item.head_revision}${item.parent ? ` · 来自 ${item.parent}` : ""}`})]),item.active ? badge("当前","success") : button("切换",{kind:"secondary",disabled:shell.active_jobs.length>0,reason:"存在活动 Job 时不能切换",onClick:async()=>{ try { await api.switchBranch(shell.task.task_id,item.branch_id); showToast(`已切换到 ${item.branch_id}`); navigate(`/tasks/${encodeURIComponent(shell.task.task_id)}`); } catch(error) { showToast(describeError(error)); } }})]))),
  ]);
}

function systemSettings() {
  const current=document.documentElement.dataset.theme;
  return element("section",{className:"card settings-panel"},[
    element("div",{className:"card__header"},[element("div",{},[element("h2",{text:"系统与显示"}),element("p",{className:"muted",text:"前后端版本与后端提交校验。"})])]),
    runtimeStatusBadges(),runtimeVersionDetails(),runtimeProbeDetails(),
    element("div",{className:"button-row"},[
      button(current==="dark" ? "切换浅色主题" : "切换深色主题",{onClick:(event)=>{ const next=document.documentElement.dataset.theme==="dark" ? "light" : "dark"; applyTheme(next); event.currentTarget.textContent=next==="dark" ? "切换浅色主题" : "切换深色主题"; }}),
      button("重新检测模型",{kind:"secondary",onClick:async(event)=>{ const control=event.currentTarget; setBusy(control,true,"正在检测…"); await refreshRuntimeStatus(true); setBusy(control,false); }}),
    ]),
  ]);
}

function renderComponents() {
  document.title = "组件规范 · PPT Agent";
  const openDialog = button("打开确认对话框", { onClick: () => document.getElementById("component-dialog").showModal() });
  const dialog = element("dialog", { id: "component-dialog", "aria-labelledby": "dialog-title" }, [
    element("div", { className: "dialog__body" }, [element("h2", { id: "dialog-title", text: "确认不可逆操作" }), element("p", { text: "对话框只用于需要阻断流程的确认。" })]),
    element("div", { className: "dialog__actions" }, [button("取消", { onClick: () => dialog.close() }), button("确认", { kind: "danger", onClick: () => dialog.close() })]),
  ]);
  const page = element("div", { className: "app-shell" }, [
    topbar(),
    element("main", { className: "home", id: "main-content", tabIndex: -1 }, [
      element("p", { className: "eyebrow", text: "基础设计系统" }),
      element("h1", { text: "组件状态与可访问行为" }),
      element("p", { className: "hero-copy", text: "所有控件使用语义 token、可见焦点、44px 触控目标，并支持深浅色与减少动态效果。" }),
      element("div", { className: "component-grid" }, [
        demoCard("按钮与图标按钮", element("div", { className: "component-row" }, [button("主操作", { kind: "primary" }), button("次操作"), button("危险操作", { kind: "danger" }), button("已禁用", { disabled: true, reason: "当前阶段不可操作" }), iconButton("moon", "主题设置", () => {})])),
        demoCard("字段与选择", element("div", {}, [fieldDemo(), selectDemo(), element("div", { className: "field" }, [element("label", { className: "field__label", htmlFor: "demo-textarea", text: "修改要求" }), element("textarea", { className: "textarea", id: "demo-textarea", placeholder: "说明修改范围与目标" })])])),
        demoCard("状态徽标", element("div", { className: "component-row" }, [badge("运行中", "primary"), badge("等待人工", "warning"), badge("已完成", "success"), badge("失败", "danger")])),
        demoCard("进度与骨架", element("div", {}, [progress(46, "46%", "正在生成页面 5 / 12"), element("div", { className: "skeleton skeleton--title" }), element("div", { className: "skeleton skeleton--line" }), element("div", { className: "skeleton skeleton--line" })])),
        demoCard("页内错误", element("div", { className: "inline-error", role: "alert" }, [element("strong", { text: "保存失败" }), element("span", { text: "请检查网络连接，最后成功版本仍可使用。" })])),
        demoCard("弹窗与消息", element("div", { className: "component-row" }, [openDialog, button("显示消息", { onClick: () => showToast("设置已保存") })])),
        demoCard("空状态", element("div", { className: "empty-state" }, [icon("file"), element("h2", { text: "暂无任务" }), element("p", { text: "创建第一个任务以开始。" }), button("新建任务", { kind: "primary", href: "/" })])),
      ]),
      dialog,
    ]),
  ]);
  replaceApp(page);
}

function demoCard(title, content) {
  return element("section", { className: "card" }, [element("div", { className: "card__header" }, element("h2", { text: title })), content]);
}

function fieldDemo() {
  return element("div", { className: "field" }, [element("label", { className: "field__label", htmlFor: "demo-input", text: "任务名称" }), element("input", { className: "input", id: "demo-input", value: "新品发布方案", "aria-describedby": "demo-hint" }), element("span", { className: "field__hint", id: "demo-hint", text: "名称会显示在任务导航中。" })]);
}

function selectDemo() {
  return element("div", { className: "field" }, [element("label", { className: "field__label", htmlFor: "demo-select", text: "运行模式" }), element("select", { className: "select", id: "demo-select" }, [element("option", { text: "人工模式" }), element("option", { text: "自动模式" })])]);
}

function renderTaskList(tasks, emptyMessage, activeId = null) {
  if (!tasks.length) return element("div", { className: "empty-state" }, [icon("file"), element("p", { text: emptyMessage })]);
  return element("ul", { className: "task-list" }, tasks.map((task) => {
    const label = STATUS[task.status]?.[0] || task.status;
    const link = element("a", { className: "task-link", href: `/tasks/${encodeURIComponent(task.task_id)}`, "aria-current": task.task_id === activeId ? "page" : null, onClick: intercept }, [
      element("span", {}, [element("strong", { text: task.task_id }), element("small", { text: `${stageLabel(task.stage)} · ${label}` })]),
      badge(taskModeLabel(task.mode, true)),
    ]);
    return element("li", {}, link);
  }));
}

function renderLoading(workspace) {
  const content = element("main", { className: workspace ? "workspace" : "home", id: "main-content", "aria-busy": "true", "aria-label": "正在加载" }, [
    element("div", { className: "workspace__inner" }, [element("div", { className: "skeleton skeleton--title" }), element("div", { className: "card", role: "status" }, [element("span", { className: "sr-only", text: "正在加载任务状态" }), element("div", { className: "skeleton skeleton--line" }), element("div", { className: "skeleton skeleton--line" })])]),
  ]);
  replaceApp(element("div", { className: "app-shell" }, [topbar(), content]));
}

function renderFatal(error, retry, taskId = null) {
  const notFound = error instanceof ApiError && error.status === 404;
  const page = element("div", { className: "app-shell" }, [
    topbar(taskId ? { task: { task_id: taskId, mode: "manual" } } : {}),
    element("main", { className: "home", id: "main-content", tabIndex: -1 }, [
      element("section", { className: "card empty-state" }, [
        icon("file"),
        element("h1", { text: notFound ? "没有找到这个任务" : navigator.onLine ? "暂时无法加载工作台" : "网络连接已断开" }),
        element("p", { text: describeError(error) }),
        error.diagnosticId ? element("p", { className: "field__hint", text: `诊断 ID：${error.diagnosticId}` }) : null,
        element("div", { className: "component-row" }, [button("重试", { kind: "primary", onClick: retry }), button("返回首页", { href: "/" })]),
      ]),
    ]),
  ]);
  replaceApp(page);
}

function openSettings() {
  const current = document.documentElement.dataset.theme;
  const dialog = element("dialog", { "aria-labelledby": "settings-title" }, [
    element("div", { className: "dialog__body" }, [
      element("p", { className: "eyebrow", text: "工作台设置" }),
      element("h2", { id: "settings-title", text: "显示与连接" }),
      element("div", { className: "field" }, [
        element("span", { className: "field__label", text: "主题" }),
        element("p", { className: "field__hint", text: `当前为${current === "dark" ? "深色" : "浅色"}主题。偏好只保存在本机浏览器。` }),
        button(current === "dark" ? "使用浅色主题" : "使用深色主题", { onClick: () => {
          const next = current === "dark" ? "light" : "dark";
          applyTheme(next);
          dialog.close();
          dialog.remove();
          const toggle = document.querySelector(`button[aria-label="${current === "dark" ? "切换浅色主题" : "切换深色主题"}"]`);
          if (toggle) {
            const label = next === "dark" ? "切换浅色主题" : "切换深色主题";
            toggle.replaceChildren(icon(next === "dark" ? "sun" : "moon"));
            toggle.setAttribute("aria-label", label);
            toggle.title = label;
          }
        } }),
      ]),
      element("div", { className: "field" }, [
        element("span", { className: "field__label", text: "服务连接" }),
        runtimeStatusBadges(),
        runtimeVersionDetails(),
        runtimeProbeDetails(),
        button("重新检测模型", { onClick: async (event) => {
          const control = event.currentTarget;
          setBusy(control, true, "正在重新检测…");
          await refreshRuntimeStatus(true);
          control.disabled = false;
          control.textContent = "重新检测模型";
          showToast(runtimeState.runtimeReady ? "模型运行时已就绪" : "模型仍不可用，请按错误代码检查配置");
        } }),
      ]),
    ]),
    element("div", { className: "dialog__actions" }, [button("关闭", { kind: "primary", onClick: () => {
      dialog.close();
      dialog.remove();
    } })]),
  ]);
  document.body.append(dialog);
  dialog.addEventListener("cancel", () => window.setTimeout(() => dialog.remove(), 0), { once: true });
  dialog.showModal();
}

function runtimeProbeDetails() {
  const container = element("div", { "data-runtime-probe-details": "true" });
  renderRuntimeProbeDetails(container);
  return container;
}

function renderRuntimeProbeDetails(container) {
  const capabilities = runtimeState.health?.model_capabilities;
  if (!capabilities?.probe_id) {
    container.replaceChildren();
    return;
  }
  const error = capabilities.error || {};
  container.replaceChildren(element("dl", { className: "metadata-list", "aria-label": "模型能力探测详情" }, [
    element("div", {}, [element("dt", { text: "探测 ID" }), element("dd", { text: capabilities.probe_id })]),
    capabilities.failed_check ? element("div", {}, [element("dt", { text: "失败检查" }), element("dd", { text: runtimeCheckLabel(capabilities.failed_check) })]) : null,
    error.probe_phase ? element("div", {}, [element("dt", { text: "失败阶段" }), element("dd", { text: runtimePhaseLabel(error.probe_phase) })]) : null,
    Number.isInteger(error.tool_calls) ? element("div", {}, [element("dt", { text: "工具调用数" }), element("dd", { text: String(error.tool_calls) })]) : null,
    error.code ? element("div", {}, [element("dt", { text: "运行时错误" }), element("dd", { text: error.code })]) : null,
    error.underlying_code ? element("div", {}, [element("dt", { text: "底层错误" }), element("dd", { text: error.underlying_code })]) : null,
    error.message ? element("div", {}, [element("dt", { text: "错误详情" }), element("dd", { text: error.message })]) : null,
  ]));
}

function runtimeCheckLabel(check) {
  return ({ basic_response: "基础文本响应", strict_json_schema: "严格 JSON Schema", tool_round_trip: "工具调用与结果回传", capability_contract: "能力契约" })[check] || check;
}

function runtimePhaseLabel(phase) {
  return ({ basic_response: "基础响应", strict_json_schema: "结构化输出", tool_request: "请求工具调用", tool_result: "回传工具结果", tool_final_output: "工具轮最终输出" })[phase] || phase;
}

function progress(value, valueLabel, step) {
  const determinate = typeof value === "number";
  const bar = element("div", {
    className: `progress__bar ${determinate ? "" : "progress__bar--indeterminate"}`,
    role: "progressbar",
    "aria-valuemin": determinate ? "0" : null,
    "aria-valuemax": determinate ? "100" : null,
    "aria-valuenow": determinate ? String(value) : null,
    "aria-label": step,
  });
  if (determinate) bar.style.setProperty("--progress", `${Math.max(0, Math.min(100, value))}%`);
  return element("div", { className: "progress" }, [element("div", { className: "progress__track" }, bar), element("div", { className: "progress__meta" }, [element("span", { text: step }), element("span", { className: "progress__value", text: valueLabel })])]);
}

function heroArt() {
  return element("div", { className: "hero-art", "aria-hidden": "true" }, [element("div", { className: "hero-orb" }), element("div", { className: "slide-stack" }, [element("span"), element("span"), element("span", {}, brandMark())])]);
}

function operationLabel(operation) {
  return ({ "clarification.generate": "AI 阅读任务卡并生成问题", "narrative.generate": "生成叙事结构", "outline.generate": "生成逐页大纲", "samples.generate": "生成样品", "samples.modify": "修改样品", "deck.generate": "生成全稿", "deck.modify": "修改全稿", "inspection.run": "执行独立检查", "inspection.fix": "修复检查问题", "delivery.publish": "写入离线包" })[operation] || operation;
}

function stageLabel(stage) {
  return ({ created: "任务/资料", clarification: "澄清", narrative: "叙事结构", outline: "逐页大纲", sample: "样品", deck: "全稿", review: "自检与修改", delivery: "交付" })[stage] || stage;
}

function replaceApp(node) {
  app.replaceChildren(node);
  app.setAttribute("aria-busy", "false");
  updateRuntimeUI();
}

function intercept(event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const anchor = event.currentTarget;
  if (!anchor?.href || anchor.origin !== window.location.origin) return;
  if (hasUnsavedDraft && !window.confirm("当前阶段有未保存修改，确定离开吗？")) return;
  hasUnsavedDraft = false;
  event.preventDefault();
  navigate(`${anchor.pathname}${anchor.search}${anchor.hash}`);
}

function describeError(error) {
  return error instanceof ApiError ? error.message : "发生未知错误，请重试。";
}
