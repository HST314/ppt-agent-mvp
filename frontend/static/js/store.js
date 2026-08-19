export function createStore(initialState = {}) {
  let state = { ...initialState };
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...patch };
      listeners.forEach((listener) => listener(state));
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

const memorySession = new Map();

function session(method, ...args) {
  try {
    return window.sessionStorage[method](...args);
  } catch (_error) {
    if (method === "getItem") return memorySession.get(args[0]) ?? null;
    if (method === "setItem") memorySession.set(args[0], String(args[1]));
    if (method === "removeItem") memorySession.delete(args[0]);
    return null;
  }
}

function sessionEntries() {
  try {
    return Array.from({ length: window.sessionStorage.length }, (_, index) => {
      const key = window.sessionStorage.key(index);
      return key ? [key, window.sessionStorage.getItem(key)] : null;
    }).filter(Boolean);
  } catch (_error) {
    return [...memorySession.entries()];
  }
}

export function intentKey(taskId, operation, payload) {
  return `ppt-agent:intent:${taskId}:${operation}:${fingerprint(stableStringify(payload))}`;
}

export function getOrCreateIdempotencyKey(taskId, operation, payload) {
  const key = intentKey(taskId, operation, payload);
  let value = session("getItem", key);
  if (!value) {
    value = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    session("setItem", key, value);
  }
  return { storageKey: key, value };
}

const JOB_INTENT_PREFIX = "ppt-agent:job-intent:";

export function bindJobIntent(job, storageKey) {
  session("setItem", `${JOB_INTENT_PREFIX}${job.job_id}`, JSON.stringify({
    taskId: job.task_id,
    storageKey,
  }));
}

export function storageKeyForJob(jobId) {
  const raw = session("getItem", `${JOB_INTENT_PREFIX}${jobId}`);
  if (!raw) return null;
  try {
    const record = JSON.parse(raw);
    return typeof record.storageKey === "string" ? record.storageKey : null;
  } catch (_error) {
    session("removeItem", `${JOB_INTENT_PREFIX}${jobId}`);
    return null;
  }
}

export function storedJobIntents(taskId) {
  const records = [];
  for (const [key, raw] of sessionEntries()) {
    if (!key?.startsWith(JOB_INTENT_PREFIX)) continue;
    try {
      const record = JSON.parse(raw);
      if (record.taskId === taskId && typeof record.storageKey === "string") {
        records.push({ jobId: key.slice(JOB_INTENT_PREFIX.length), storageKey: record.storageKey });
      }
    } catch (_error) {
      session("removeItem", key);
    }
  }
  return records;
}

export function clearIdempotencyKey(storageKey, jobId = null) {
  session("removeItem", storageKey);
  if (jobId) session("removeItem", `${JOB_INTENT_PREFIX}${jobId}`);
}

function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function fingerprint(value) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}
