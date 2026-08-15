const JSON_HEADERS = { "Content-Type": "application/json" };

export class ApiError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = "ApiError";
    this.code = details.code || "request_failed";
    this.diagnosticId = details.diagnostic_id || null;
    this.agentAuditId = details.agent_audit_id || null;
    this.retryable = details.retryable === true;
    this.retryAfterSeconds = details.retry_after_seconds || null;
    this.runtimeErrorCode = details.runtime_error_code || null;
    this.status = details.status || 0;
  }
}

export async function request(path, options = {}) {
  const controller = options.controller || new AbortController();
  const timeout = window.setTimeout(() => controller.abort("timeout"), options.timeout || 120_000);
  try {
    const response = await fetch(path, {
      method: options.method || "GET",
      headers: options.body === undefined ? options.headers : { ...JSON_HEADERS, ...options.headers },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data?.error || {};
      throw new ApiError(detail.message || "请求失败，请稍后重试", { ...detail, status: response.status });
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) {
      throw new ApiError(controller.signal.reason === "timeout" ? "请求超时，请先核对任务状态再重试" : "请求已取消", { code: "request_aborted" });
    }
    throw new ApiError(navigator.onLine ? "无法连接服务，请稍后重试" : "网络已断开，恢复连接后可继续", { code: "offline" });
  } finally {
    window.clearTimeout(timeout);
  }
}

export const api = {
  runtimeStatus: () => runtimeStatus(),
  recheckRuntime: () => runtimeStatus(true),
  listTasks: (controller) => request("/v1/tasks", { controller }),
  createTask: (payload) => request("/v1/tasks", { method: "POST", body: payload }),
  shell: (taskId, controller) => request(`/v1/tasks/${encodeURIComponent(taskId)}/shell`, { controller }),
  task: (taskId, controller) => request(taskPath(taskId), { controller }),
  input: (taskId, controller) => request(`${taskPath(taskId)}/input`, { controller }),
  importInput: (taskId, payload) => post(`${taskPath(taskId)}/input`, payload),
  retryClarification: (taskId, idempotencyKey) => post(`${taskPath(taskId)}/clarifications/retry`, { idempotency_key: idempotencyKey }),
  useFallbackClarification: (taskId) => post(`${taskPath(taskId)}/clarifications/fallback`, { confirm: true }),
  answerClarification: (taskId, questionId, payload) => post(`${taskPath(taskId)}/clarifications/${encodeURIComponent(questionId)}/answer`, payload),
  answerClarifications: (taskId, answers) => post(`${taskPath(taskId)}/clarifications/answers`, { answers }),
  planning: (taskId, controller) => request(`${taskPath(taskId)}/planning`, { controller }),
  editNarrative: (taskId, payload) => post(`${taskPath(taskId)}/narrative`, payload),
  confirmNarrative: (taskId) => post(`${taskPath(taskId)}/narrative/confirm`, {}),
  editOutline: (taskId, payload) => post(`${taskPath(taskId)}/outline`, payload),
  confirmOutline: (taskId) => post(`${taskPath(taskId)}/outline/confirm`, {}),
  rollbackPlanning: (taskId, payload) => post(`${taskPath(taskId)}/planning/rollback`, payload),
  samples: (taskId, controller) => request(`${taskPath(taskId)}/samples`, { controller }),
  selectSamples: (taskId, payload) => post(`${taskPath(taskId)}/samples/select`, payload),
  confirmSample: (taskId) => post(`${taskPath(taskId)}/samples/confirm`, {}),
  deck: (taskId, controller) => request(`${taskPath(taskId)}/deck`, { controller }),
  rollbackDeck: (taskId, hash) => post(`${taskPath(taskId)}/deck/rollback`, { hash }),
  compareDeck: (taskId, payload) => post(`${taskPath(taskId)}/deck/compare`, payload),
  inspection: (taskId, controller) => request(`${taskPath(taskId)}/inspection`, { controller }),
  setInspectionMode: (taskId, mode) => post(`${taskPath(taskId)}/inspection/mode`, { mode }),
  checkDeliveryGate: (taskId) => post(`${taskPath(taskId)}/inspection/delivery-gate`, {}),
  disposeIssue: (taskId, issueId, payload) => post(`${taskPath(taskId)}/issues/${encodeURIComponent(issueId)}/disposition`, payload),
  disposeIssues: (taskId, payload) => post(`${taskPath(taskId)}/issues/dispositions/batch`, payload),
  delivery: (taskId, controller) => request(`${taskPath(taskId)}/delivery`, { controller }),
  confirmDelivery: (taskId, payload) => post(`${taskPath(taskId)}/delivery/confirm`, payload),
  deriveDelivery: (taskId, payload) => post(`${taskPath(taskId)}/delivery/derive`, payload),
  versions: (taskId, controller) => request(`${taskPath(taskId)}/versions`, { controller }),
  version: (taskId, hash, controller) => request(`${taskPath(taskId)}/versions/${encodeURIComponent(hash)}`, { controller }),
  compareVersions: (taskId, payload) => post(`${taskPath(taskId)}/versions/compare`, payload),
  getJob: (jobId) => request(`/v1/jobs/${encodeURIComponent(jobId)}`),
  activeJobs: (taskId) => request(`/v1/tasks/${encodeURIComponent(taskId)}/jobs?status=active`),
  createJob: (taskId, payload) => request(`/v1/tasks/${encodeURIComponent(taskId)}/jobs`, { method: "POST", body: payload }),
  cancelJob: (jobId) => request(`/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST", body: {} }),
};

async function runtimeStatus(recheck = false) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort("timeout"), 8000);
  try {
    const live = await fetch("/livez", { signal: controller.signal, cache: "no-store" });
    const liveData = await live.json().catch(() => ({}));
    if (!live.ok) return { backendReachable: false, runtimeReady: false, live: liveData, ready: null };
    const ready = await fetch(recheck ? "/v1/runtime/recheck" : "/v1/runtime/status", {
      method: recheck ? "POST" : "GET",
      signal: controller.signal,
      cache: "no-store",
    });
    const readyData = await ready.json().catch(() => ({}));
    return { backendReachable: true, runtimeReady: readyData.runtime_ready === true, live: liveData, ready: readyData };
  } catch (_error) {
    return { backendReachable: false, runtimeReady: false, live: null, ready: null };
  } finally {
    window.clearTimeout(timeout);
  }
}

function taskPath(taskId) {
  return `/v1/tasks/${encodeURIComponent(taskId)}`;
}

function post(path, body) {
  return request(path, { method: "POST", body });
}
