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

export function intentKey(taskId, operation, payload) {
  return `ppt-agent:intent:${taskId}:${operation}:${fingerprint(stableStringify(payload))}`;
}

export function getOrCreateIdempotencyKey(taskId, operation, payload) {
  const key = intentKey(taskId, operation, payload);
  let value = sessionStorage.getItem(key);
  if (!value) {
    value = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    sessionStorage.setItem(key, value);
  }
  return { storageKey: key, value };
}

export function clearIdempotencyKey(storageKey) {
  sessionStorage.removeItem(storageKey);
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
