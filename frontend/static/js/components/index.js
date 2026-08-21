import { badge, button, element, icon } from "../shell.js?v=2026.08.21.035240047774";

export { badge, button, element, icon };

export function field(label, control, { hint = "", error = null } = {}) {
  const controlId = control.id || `field-${Math.random().toString(16).slice(2)}`;
  control.id = controlId;
  const hintId = `${controlId}-hint`;
  const errorId = `${controlId}-error`;
  const described = [];
  if (hint) described.push(hintId);
  if (error) described.push(errorId);
  if (described.length) control.setAttribute("aria-describedby", described.join(" "));
  return element("div", { className: "field" }, [
    element("label", { className: "field__label", htmlFor: controlId, text: label }),
    control,
    hint ? element("span", { className: "field__hint", id: hintId, text: hint }) : null,
    error || element("p", { className: "field__error", id: errorId, role: "alert" }),
  ]);
}

export function inlineError(message, diagnosticId = null) {
  return element("div", { className: "inline-error", role: "alert" }, [
    element("strong", { text: "操作未完成" }),
    element("span", { text: message }),
    diagnosticId ? element("small", { text: `诊断 ID：${diagnosticId}` }) : null,
  ]);
}

export function emptyState(title, description, action = null) {
  return element("section", { className: "card empty-state" }, [
    icon("file"),
    element("h2", { text: title }),
    element("p", { text: description }),
    action,
  ]);
}

export function shortHash(value) {
  return value ? `${String(value).slice(0, 12)}…` : "—";
}

export function formatTime(value) {
  if (!value) return "时间未知";
  try {
    return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
  } catch (_error) {
    return String(value);
  }
}

export function metadataList(items) {
  return element("dl", { className: "metadata-list" }, items.filter((item) => item[1] !== undefined && item[1] !== null).flatMap(([label, value]) => [
    element("dt", { text: label }),
    element("dd", { text: String(value) }),
  ]));
}

const PREVIEW_CANVAS_WIDTH = 1280;
const PREVIEW_CANVAS_HEIGHT = 720;

export function previewFrame(html, title, { id = "", allowInspection = false, src = "" } = {}) {
  const frame = element("iframe", {
    className: "preview-frame",
    id,
    title,
    loading: "lazy",
    sandbox: allowInspection ? "allow-same-origin" : "",
  });
  frame.src = src || "about:blank";
  const aspect = element("div", { className: "preview-aspect" }, frame);
  scalePreviewToFit(aspect, frame);
  return aspect;
}

/**
 * Keep the iframe at the logical 1280×720 canvas and scale the whole frame to
 * fit the container width (or the full viewport in fullscreen), so every
 * preview shows the complete canvas instead of a clipped region.
 */
export function scalePreviewToFit(aspect, frame) {
  const apply = () => {
    const width = aspect.clientWidth;
    if (!width) return;
    const fullscreen = document.fullscreenElement === aspect;
    const scale = fullscreen
      ? Math.min(width / PREVIEW_CANVAS_WIDTH, aspect.clientHeight / PREVIEW_CANVAS_HEIGHT)
      : width / PREVIEW_CANVAS_WIDTH;
    frame.style.transform = `scale(${scale})`;
    if (!fullscreen) {
      // 全局 box-sizing: border-box：显式高度包含边框，需补偿后内容区才正好等于缩放画布高度
      const computed = getComputedStyle(aspect);
      const borders = (parseFloat(computed.borderTopWidth) || 0) + (parseFloat(computed.borderBottomWidth) || 0);
      aspect.style.height = `${PREVIEW_CANVAS_HEIGHT * scale + borders}px`;
    }
  };
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(apply);
    observer.observe(aspect);
  }
  document.addEventListener("fullscreenchange", apply);
  apply();
}

export function previewUrl(taskId, hash) {
  return `/v1/tasks/${encodeURIComponent(taskId)}/previews/${encodeURIComponent(hash)}`;
}

export function versionTimeline(versions, kind, { currentHash, onPreview, onRollback } = {}) {
  const filtered = versions.filter((item) => item.kind === kind).slice().reverse();
  if (!filtered.length) return element("p", { className: "muted", text: "尚无历史版本。" });
  return element("ol", { className: "version-list" }, filtered.map((item, index) => {
    const meta = item.metadata || {};
    const isCurrent = item.hash === currentHash;
    return element("li", { className: `version-item ${isCurrent ? "version-item--current" : ""}` }, [
      element("div", { className: "version-item__header" }, [
        element("strong", { text: `v${filtered.length - index}` }),
        isCurrent ? badge("当前", "primary") : null,
      ]),
      element("p", { text: meta.summary || meta.action || "版本记录" }),
      element("small", { className: "muted", text: `${meta.operator || meta.author || "system"} · ${shortHash(item.hash)}` }),
      element("div", { className: "button-row" }, [
        onPreview ? button("预览", { kind: "ghost", onClick: () => onPreview(item) }) : null,
        onRollback && !isCurrent ? button("非破坏回退", { kind: "secondary", mutates: true, onClick: () => onRollback(item) }) : null,
      ]),
    ]);
  }));
}

export function confirmationDialog({ title, description, confirmLabel = "确认", danger = false, onConfirm }) {
  const dialog = element("dialog", { "aria-labelledby": "confirm-dialog-title" }, [
    element("div", { className: "dialog__body" }, [
      element("h2", { id: "confirm-dialog-title", text: title }),
      element("p", { text: description }),
    ]),
  ]);
  const close = () => {
    dialog.close();
    dialog.remove();
  };
  const confirm = button(confirmLabel, { kind: danger ? "danger" : "primary", onClick: async () => {
    confirm.disabled = true;
    try {
      const afterConfirm = await onConfirm();
      close();
      if (typeof afterConfirm === "function") afterConfirm();
    } catch (_error) {
      confirm.disabled = false;
    }
  } });
  dialog.append(element("div", { className: "dialog__actions" }, [button("取消", { onClick: close }), confirm]));
  document.body.append(dialog);
  dialog.addEventListener("cancel", () => window.setTimeout(() => dialog.remove(), 0), { once: true });
  dialog.showModal();
  return dialog;
}

export function setBusy(buttonNode, busy, busyLabel = "正在提交…") {
  if (!buttonNode) return;
  if (busy) {
    buttonNode.dataset.label = buttonNode.textContent;
    buttonNode.textContent = busyLabel;
    buttonNode.disabled = true;
  } else {
    buttonNode.textContent = buttonNode.dataset.label || buttonNode.textContent;
    // 独立的版本阻断状态优先：忙碌恢复不得解除版本门禁的禁用。
    buttonNode.disabled = buttonNode.dataset.versionDisabled === "true";
    delete buttonNode.dataset.label;
  }
}
