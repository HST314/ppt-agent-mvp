const JSON_HEADERS = { "Content-Type": "application/json" };

export class ApiError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = "ApiError";
    this.code = details.code || "request_failed";
    this.diagnosticId = details.diagnostic_id || null;
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
  listTasks: (controller) => request("/v1/tasks", { controller }),
  createTask: (payload) => request("/v1/tasks", { method: "POST", body: payload }),
  shell: (taskId, controller) => request(`/v1/tasks/${encodeURIComponent(taskId)}/shell`, { controller }),
  getJob: (jobId) => request(`/v1/jobs/${encodeURIComponent(jobId)}`),
  activeJobs: (taskId) => request(`/v1/tasks/${encodeURIComponent(taskId)}/jobs?status=active`),
  createJob: (taskId, payload) => request(`/v1/tasks/${encodeURIComponent(taskId)}/jobs`, { method: "POST", body: payload }),
  cancelJob: (jobId) => request(`/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST", body: {} }),
};
