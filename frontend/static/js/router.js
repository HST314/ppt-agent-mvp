const LEGACY_STAGES = new Map([
  ["input", "created"],
  ["created", "created"],
  ["clarification", "clarification"],
  ["narrative", "narrative"],
  ["outline", "outline"],
  ["samples", "sample"],
  ["sample", "sample"],
  ["deck", "deck"],
  ["inspection", "review"],
  ["review", "review"],
  ["delivery", "delivery"],
]);

export function currentRoute() {
  const url = new URL(window.location.href);
  if (url.pathname === "/components") return { name: "components" };
  const match = url.pathname.match(/^\/tasks\/([^/]+)(?:\/([^/]+))?\/?$/);
  if (!match) return { name: "home" };
  const taskId = decodeURIComponent(match[1]);
  const legacy = match[2];
  const stage = legacy ? LEGACY_STAGES.get(legacy) : url.searchParams.get("stage");
  if (legacy && stage) {
    const canonical = `/tasks/${encodeURIComponent(taskId)}?stage=${encodeURIComponent(stage)}`;
    window.history.replaceState({}, "", canonical);
  }
  return { name: "workspace", taskId, stage: stage || null };
}

export function navigate(path, { replace = false } = {}) {
  window.history[replace ? "replaceState" : "pushState"]({}, "", path);
  window.dispatchEvent(new CustomEvent("app:navigate"));
}

export function installRouter(render) {
  const handler = () => render(currentRoute());
  window.addEventListener("popstate", handler);
  window.addEventListener("app:navigate", handler);
  return () => {
    window.removeEventListener("popstate", handler);
    window.removeEventListener("app:navigate", handler);
  };
}
