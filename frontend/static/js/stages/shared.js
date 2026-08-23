import { ApiError } from "../api.js?v=2026.08.23.102655140222";
import { button, element, inlineError, setBusy } from "../components/index.js?v=2026.08.23.102655140222";

export function stageGrid(primary, aside, className = "") {
  return element("div", { className: `stage-grid ${className}`.trim() }, [
    element("div", { className: "stage-grid__main" }, primary),
    element("aside", { className: "stage-grid__aside", "aria-label": "阶段上下文" }, aside),
  ]);
}

export function section(title, content, { description = "", actions = [], className = "" } = {}) {
  return element("section", { className: `card stage-section ${className}`.trim() }, [
    element("div", { className: "card__header" }, [
      element("div", {}, [element("h2", { text: title }), description ? element("p", { className: "muted", text: description }) : null]),
      actions.length ? element("div", { className: "button-row" }, actions) : null,
    ]),
    ...asArray(content),
  ]);
}

export function actionMessage() {
  return element("div", { className: "stage-message", role: "status", "aria-live": "polite" });
}

export function showActionError(region, error) {
  region.replaceChildren(inlineError(error instanceof ApiError ? error.message : "操作失败，请重试。", error?.diagnosticId));
}

let versionMatchGuard = null;

export function setVersionMatchGuard(guard) {
  versionMatchGuard = typeof guard === "function" ? guard : null;
}

export async function runAction({ buttonNode, region, action, success, refresh, busyLabel, requiresVersionMatch = false }) {
  region?.replaceChildren();
  setBusy(buttonNode, true, busyLabel);
  try {
    if (requiresVersionMatch && versionMatchGuard) await versionMatchGuard();
    const result = await action();
    if (success) region?.append(element("p", { className: "success-message", text: success }));
    if (refresh) await refresh(result);
    return result;
  } catch (error) {
    if (region) showActionError(region, error);
    throw error;
  } finally {
    setBusy(buttonNode, false);
  }
}

export function primaryAction(label, handler, options = {}) {
  return button(label, { kind: "primary", onClick: handler, ...options });
}

export function codeBlock(value, label = "内容") {
  return element("pre", { className: "code-block", tabIndex: 0, "aria-label": label, text: value || "" });
}

export function hashBadge(value) {
  return element("code", { className: "hash", text: value || "—", title: value || "" });
}

export function invalidationNotice(items) {
  if (!items?.length) return null;
  return element("div", { className: "notice notice--warning", role: "status" }, [
    element("strong", { text: "下游内容已失效" }),
    element("p", { text: `本次变更要求重新确认或生成：${items.join("、")}。` }),
  ]);
}

export function draftGuard(textarea, baseline, context) {
  const update = () => context.setDirty(textarea.value !== baseline);
  textarea.addEventListener("input", update);
  return update;
}

export function emptyCopy(title, description, action) {
  return element("div", { className: "empty-state empty-state--compact" }, [
    element("h2", { text: title }),
    element("p", { text: description }),
    action,
  ]);
}

export function choiceOptions(options, selected) {
  return options.map((option) => element("option", { value: option, text: option, selected: option === selected }));
}

export function parseSlideIds(value) {
  return [...new Set(String(value || "").split(/[\s,，]+/).map((item) => item.trim()).filter(Boolean))];
}

function asArray(value) {
  if (Array.isArray(value)) return value;
  return value ? [value] : [];
}
