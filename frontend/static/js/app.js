import { api, ApiError } from "./api.js?v=2026.08.15.155434751550";
import { JobTracker } from "./job-tracker.js?v=2026.08.15.155434751550";
import { currentRoute, installRouter, navigate } from "./router.js?v=2026.08.15.155434751550";
import { applyTheme, badge, brandMark, button, element, icon, iconButton, preferredTheme, showToast } from "./shell.js?v=2026.08.15.155434751550";
import { bindJobIntent, clearIdempotencyKey, getOrCreateIdempotencyKey, storageKeyForJob, storedJobIntents } from "./store.js?v=2026.08.15.155434751550";
import { inlineError, setBusy } from "./components/index.js?v=2026.08.15.155434751550";
import { renderStage } from "./stages/index.js?v=2026.08.15.155434751550";

const app = document.getElementById("app");
const APP_BUILD = document.querySelector('meta[name="app-build"]')?.content || "unknown";
const tracker = new JobTracker();
let renderGeneration = 0;
let activeController = null;
let hasUnsavedDraft = false;
let acceptedLocation = `${window.location.pathname}${window.location.search}${window.location.hash}`;
let runtimeProbe = null;
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
renderRoute(currentRoute());
refreshRuntimeStatus();
window.setInterval(() => refreshRuntimeStatus(), 15_000);

async function renderRoute(route) {
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
  if (route.name === "home") await renderHome(generation);
  else if (route.name === "components") renderComponents();
  else await renderWorkspace(route, generation);
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
  const home = element("a", { className: "topbar__brand", href: "/", onClick: intercept }, [
    brandMark(),
    element("span", { className: "topbar__title", text: "PPT Agent" }),
  ]);
  const connection = runtimeStatusBadges();
  const build = badge(`Build ${APP_BUILD}`, "primary");
  build.classList.add("badge--build");
  const settingsButton = iconButton("settings", "打开设置", openSettings);
  const bar = element("header", { className: "topbar" }, [
    context.onMenu ? iconButton("menu", "打开任务与阶段导航", context.onMenu, "menu-button") : null,
    home,
    context.task ? element("div", { className: "topbar__context" }, [
      element("span", { className: "context-label", text: "当前任务" }),
      element("strong", { text: context.task.task_id }),
      badge(context.task.mode === "auto" ? "自动模式" : "人工模式"),
    ]) : null,
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

function runtimeStatusBadges() {
  const group = element("div", { className: "runtime-status badge--connection", "data-runtime-status": "true", role: "status", "aria-live": "polite" });
  renderRuntimeBadges(group);
  return group;
}

function renderRuntimeBadges(group) {
  const browserLabel = runtimeState.browserOnline ? "浏览器在线" : "浏览器离线";
  const browserTone = runtimeState.browserOnline ? "success" : "danger";
  const backendLabel = runtimeState.backendReachable === null ? "后端检测中" : runtimeState.backendReachable ? "后端可达" : "后端不可达";
  const backendTone = runtimeState.backendReachable === null ? "warning" : runtimeState.backendReachable ? "success" : "danger";
  let modelLabel = "模型检测中";
  let modelTone = "warning";
  if (runtimeState.health?.clarification_mode === "fake" && runtimeState.runtimeReady) {
    modelLabel = "模型：本地模式";
    modelTone = "primary";
  } else if (runtimeState.runtimeReady === true) {
    modelLabel = "模型可用";
    modelTone = "success";
  } else if (runtimeState.runtimeReady === false) {
    modelLabel = "模型不可用";
    modelTone = "danger";
  }
  const signature = JSON.stringify([browserLabel, browserTone, backendLabel, backendTone, modelLabel, modelTone]);
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
  group.replaceChildren(browser, backend, model);
}

async function refreshRuntimeStatus(recheck = false) {
  if (runtimeProbe && !recheck) return runtimeProbe;
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
  document.querySelectorAll('[data-requires-runtime="true"]').forEach((control) => {
    if (runtimeState.runtimeReady) {
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
    const reason = runtimeState.backendReachable === false ? "后端服务当前不可达" : "模型运行时不可用，请先重新检测";
    control.title = reason;
    control.setAttribute("aria-description", reason);
  });
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
    element("option", { value: "auto", text: "自动推进到人工门禁" }),
  ]);
  const error = element("p", { className: "field__error", id: "create-error", role: "alert" });
  const submit = button("创建任务并进入工作台", { kind: "primary", type: "submit", block: true });
  const form = element("form", { onSubmit: async (event) => {
    event.preventDefault();
    error.textContent = "";
    submit.disabled = true;
    submit.textContent = "正在创建任务…";
    try {
      const created = await api.createTask({ task_id: taskId.value.trim(), mode: mode.value });
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
    error,
    submit,
  ]);
  return element("section", { className: "card card--raised", "aria-labelledby": "create-heading" }, [
    element("div", { className: "card__header" }, [element("h2", { id: "create-heading", text: "新建 PPT 任务" }), badge("第一步", "primary")]),
    form,
  ]);
}

async function renderWorkspace(route, generation) {
  renderLoading(true);
  try {
    const [shell, recent] = await Promise.all([
      api.shell(route.taskId, activeController),
      api.listTasks(activeController),
    ]);
    if (generation !== renderGeneration) return;
    document.title = `${shell.task.task_id} · PPT Agent`;
    let sidebar;
    let scrim;
    const closeDrawer = () => {
      sidebar.dataset.open = "false";
      scrim.dataset.open = "false";
    };
    const openDrawer = () => {
      sidebar.dataset.open = "true";
      scrim.dataset.open = "true";
      sidebar.querySelector("button, a")?.focus();
    };
    const selected = shell.stages.find((stage) => stage.id === route.stage) || shell.stages.find((stage) => stage.status === "current");
    sidebar = workspaceSidebar(shell, recent.tasks, selected, closeDrawer);
    scrim = element("button", { className: "drawer-scrim", type: "button", "aria-label": "关闭导航", onClick: closeDrawer });
    const page = element("div", { className: "app-shell" }, [
      topbar({ task: shell.task, onMenu: openDrawer }),
      element("div", { className: "shell-grid" }, [
        sidebar,
        workspaceMain(shell, selected),
      ]),
      scrim,
    ]);
    replaceApp(page);
    shell.active_jobs.forEach((job) => connectJob(job, route));
    await reconcileStoredIntents(shell.task.task_id, shell.active_jobs, route);
    if (generation !== renderGeneration) return;
    if (!lockedStage(selected)) {
      try {
        const content = await renderStage(selected.id, stageContext(shell, selected, route, generation));
        if (generation !== renderGeneration) return;
        enforceTaskActionState(content, shell.task);
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
  const stages = element("ol", { className: "stage-list" });
  shell.stages.forEach((stage, index) => {
    const isSelected = stage.id === selected.id;
    const label = element("span", { className: "stage-label" }, [
      element("span", { text: stage.label }),
      stage.lock_reason ? element("small", { text: stage.lock_reason.replace("前置条件：", "") }) : null,
    ]);
    const link = element("a", {
      className: "stage-link",
      href: stage.href,
      "aria-current": isSelected ? "step" : null,
      "aria-disabled": stage.status === "locked" ? "true" : null,
      "data-status": stage.status,
      title: stage.lock_reason,
      onClick: (event) => {
        if (stage.status === "locked") {
          event.preventDefault();
          showToast(stage.lock_reason);
          return;
        }
        intercept(event);
        closeDrawer();
      },
    }, [element("span", { className: "stage-index", text: stage.status === "completed" ? "✓" : String(index + 1) }), label]);
    stages.append(element("li", { className: "stage-item" }, link));
  });
  return element("aside", { className: "sidebar", "aria-label": "任务与阶段导航", "data-open": "false", onKeydown: (event) => {
    if (event.key === "Escape") closeDrawer();
  } }, [
    iconButton("close", "关闭导航", closeDrawer, "sidebar__close"),
    element("div", { className: "sidebar__section" }, [button("新建任务", { href: "/", kind: "primary", block: true })]),
    element("section", { className: "sidebar__section", "aria-labelledby": "stage-heading" }, [
      element("h2", { className: "sidebar__heading", id: "stage-heading", text: "8 阶段工作流" }),
      stages,
    ]),
    element("section", { className: "sidebar__section", "aria-labelledby": "tasks-heading" }, [
      element("h2", { className: "sidebar__heading", id: "tasks-heading", text: "最近任务" }),
      renderTaskList(recent.slice(0, 8), "暂无其他任务", shell.task.task_id),
    ]),
  ]);
}

function workspaceMain(shell, selected) {
  const current = shell.stages.find((stage) => stage.status === "current");
  const locked = selected.status === "locked";
  const [statusLabel, tone] = STATUS[shell.task.status] || [shell.task.status, ""];
  const main = element("main", { className: "workspace", id: "main-content", tabIndex: -1 }, [
    element("div", { className: "workspace__inner" }, [
      element("header", { className: "workspace-header" }, [
        element("div", {}, [
          element("p", { className: "eyebrow", text: `阶段 ${shell.stages.findIndex((stage) => stage.id === selected.id) + 1} / 8` }),
          element("h1", { text: selected.label }),
          element("p", { text: locked ? selected.lock_reason : selected.status === "available" ? `该阶段已满足前置门禁；任务当前阶段仍为“${current.label}”。` : selected.id === current.id ? "这是任务当前的权威阶段。" : `正在查看已完成阶段；当前阶段为“${current.label}”。` }),
        ]),
        badge(statusLabel, tone),
      ]),
      element("div", { id: "active-job-region", "aria-live": "polite" }, shell.active_jobs.map(jobPanel)),
      element("div", { id: "stage-content" }, locked ? lockedState(selected, current) : [
        element("div", { className: "skeleton skeleton--title" }),
        element("div", { className: "card", role: "status" }, [element("span", { className: "sr-only", text: `正在加载${selected.label}` }), element("div", { className: "skeleton skeleton--line" }), element("div", { className: "skeleton skeleton--line" })]),
      ]),
    ]),
  ]);
  window.requestAnimationFrame(() => main.focus({ preventScroll: true }));
  return main;
}

function lockedState(selected, current) {
  return element("section", { className: "card empty-state" }, [
    icon("file"),
    element("h2", { text: "该阶段尚未解锁" }),
    element("p", { text: selected.lock_reason }),
    button(`返回当前阶段：${current.label}`, { href: current.href, kind: "primary" }),
  ]);
}

function jobPanel(job) {
  const value = typeof job.progress === "number" ? job.progress : 0;
  return element("section", { className: "job-panel", id: `job-${job.job_id}`, "aria-label": "活动后台任务" }, [
    element("div", { className: "job-panel__header" }, [
      element("div", {}, [element("strong", { text: operationLabel(job.operation) }), element("p", { text: job.current_step || "正在准备" })]),
      badge(job.status === "queued" ? "排队中" : "执行中", "primary"),
    ]),
    progress(value, typeof job.progress === "number" ? `${job.progress}%` : "进度以真实检查点为准", job.current_step || "运行中"),
    element("p", { className: "field__hint", text: "可以安全离开此页；再次打开任务时会自动恢复显示。" }),
    job.operation !== "clarification.generate" && !["succeeded", "failed", "cancelled", "interrupted"].includes(job.status) ? button(job.cancellation_requested ? "正在取消" : "取消后台任务", { kind: "ghost", disabled: job.cancellation_requested, onClick: async () => {
      try {
        const next = await api.cancelJob(job.job_id);
        updateJobPanel(next);
      } catch (error) {
        showToast(describeError(error));
      }
    } }) : null,
    job.error ? inlineError(job.error.message || "后台任务失败", job.error.diagnostic_id) : null,
  ]);
}

function connectJob(job, route, storageKey = null) {
  const recoveredStorageKey = storageKey || storageKeyForJob(job.job_id);
  tracker.track(job, {
    onUpdate: (next) => updateJobPanel(next),
    onEvent: (event) => updateJobEvent(event),
    onComplete: (finished) => {
      if (recoveredStorageKey) clearIdempotencyKey(recoveredStorageKey, finished.job_id);
      showToast(finished.status === "succeeded" ? "后台任务已完成" : "后台任务已结束，请查看详情");
      renderRoute(route);
    },
  });
}

async function reconcileStoredIntents(taskId, activeJobs, route) {
  const activeIds = new Set(activeJobs.map((job) => job.job_id));
  const stored = storedJobIntents(taskId).filter((item) => !activeIds.has(item.jobId));
  await Promise.all(stored.map(async ({ jobId, storageKey }) => {
    try {
      const job = await api.getJob(jobId);
      if (["succeeded", "failed", "cancelled", "interrupted"].includes(job.status)) {
        clearIdempotencyKey(storageKey, jobId);
      } else {
        connectJob(job, route, storageKey);
      }
    } catch (_error) {
      clearIdempotencyKey(storageKey, jobId);
    }
  }));
}

function stageContext(shell, selected, route, generation) {
  return {
    taskId: shell.task.task_id,
    shell,
    selected,
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
  if (job && !["succeeded", "failed", "cancelled", "interrupted"].includes(job.status)) renderRoute(route);
  return job;
}

async function startTrackedJob({ taskId, route, buttonNode, region, intent, create, busyLabel = "正在创建后台任务…" }) {
  region?.replaceChildren();
  setBusy(buttonNode, true, busyLabel);
  try {
    await refreshRuntimeStatus();
    if (!runtimeState.runtimeReady) {
      throw new ApiError("模型运行时不可用，请先在连接状态中重新检测", { code: "runtime_unavailable", status: 503 });
    }
    const job = await create();
    bindJobIntent(job, intent.storageKey);
    let activeRegion = document.getElementById("active-job-region");
    if (!activeRegion) activeRegion = region;
    const existing = document.getElementById(`job-${job.job_id}`);
    if (existing) existing.replaceWith(jobPanel(job));
    else activeRegion?.append(jobPanel(job));
    connectJob(job, route, intent.storageKey);
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

function enforceTaskActionState(content, task) {
  if (!["paused", "cancelled", "failed", "completed"].includes(task.status)) return;
  content.querySelectorAll('[data-mutates="true"]').forEach((control) => {
    if (task.status === "completed" && control.dataset.allowCompleted === "true") return;
    control.disabled = true;
    control.title = `任务状态 ${task.status} 不允许执行该动作`;
    control.setAttribute("aria-description", control.title);
  });
}

function updateJobPanel(job) {
  const old = document.getElementById(`job-${job.job_id}`);
  if (old) old.replaceWith(jobPanel(job));
}

function updateJobEvent(event) {
  const panel = document.getElementById(`job-${event.job_id}`);
  if (!panel) return;
  const text = panel.querySelector(".job-panel__header p");
  if (text && event.message) text.textContent = event.message;
  const bar = panel.querySelector(".progress__bar");
  if (bar && typeof event.progress === "number") bar.style.setProperty("--progress", `${event.progress}%`);
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
      badge(task.mode === "auto" ? "自动" : "人工"),
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
        element("p", { className: "field__hint", text: runtimeVersionText() }),
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

function runtimeVersionText() {
  const commit = runtimeState.health?.backend_commit || "unknown";
  const config = runtimeState.health?.config_summary_sha256 || "unknown";
  return `前端 Build ${APP_BUILD} · 后端 ${commit.slice(0, 12)} · 配置 ${config.slice(0, 12)}`;
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
  ]));
}

function runtimeCheckLabel(check) {
  return ({ basic_response: "基础文本响应", strict_json_schema: "严格 JSON Schema", tool_round_trip: "工具调用与结果回传", capability_contract: "能力契约" })[check] || check;
}

function runtimePhaseLabel(phase) {
  return ({ basic_response: "基础响应", strict_json_schema: "结构化输出", tool_request: "请求工具调用", tool_result: "回传工具结果", tool_final_output: "工具轮最终输出" })[phase] || phase;
}

function progress(value, valueLabel, step) {
  const bar = element("div", { className: "progress__bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(value), "aria-label": step });
  bar.style.setProperty("--progress", `${Math.max(0, Math.min(100, value))}%`);
  return element("div", { className: "progress" }, [element("div", { className: "progress__track" }, bar), element("div", { className: "progress__meta" }, [element("span", { text: step }), element("span", { text: valueLabel })])]);
}

function heroArt() {
  return element("div", { className: "hero-art", "aria-hidden": "true" }, [element("div", { className: "hero-orb" }), element("div", { className: "slide-stack" }, [element("span"), element("span"), element("span", {}, brandMark())])]);
}

function operationLabel(operation) {
  return ({ "clarification.generate": "AI 阅读任务卡并生成问题", "narrative.generate": "生成叙事结构", "outline.generate": "生成逐页大纲", "samples.generate": "生成样品", "samples.modify": "修改样品", "deck.generate": "生成全稿", "deck.modify": "修改全稿", "inspection.run": "执行独立检查" })[operation] || operation;
}

function stageLabel(stage) {
  return ({ created: "任务/资料", clarification: "澄清", narrative: "叙事结构", outline: "逐页大纲", sample: "样品", deck: "全稿", review: "检查", delivery: "交付" })[stage] || stage;
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
