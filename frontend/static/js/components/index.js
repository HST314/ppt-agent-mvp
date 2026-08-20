import { badge, button, element, icon } from "../shell.js?v=2026.08.20.114142303041";

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

export function previewFrame(html, title, { id = "", allowInspection = false, src = "" } = {}) {
  const frame = element("iframe", {
    className: "preview-frame",
    id,
    title,
    loading: "lazy",
    sandbox: allowInspection ? "allow-same-origin" : "",
  });
  frame.src = src || "about:blank";
  return element("div", { className: "preview-aspect" }, frame);
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
    buttonNode.disabled = false;
    delete buttonNode.dataset.label;
  }
}
