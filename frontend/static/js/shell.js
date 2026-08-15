const SVG_NS = "http://www.w3.org/2000/svg";

export function element(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(options).forEach(([key, value]) => {
    if (value === undefined || value === null) return;
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in node && !key.startsWith("aria")) node[key] = value;
    else node.setAttribute(key, value);
  });
  const list = Array.isArray(children) ? children : [children];
  list.filter(Boolean).forEach((child) => node.append(child));
  return node;
}

export function icon(name) {
  const paths = {
    menu: ["M4 7h16", "M4 12h16", "M4 17h16"],
    close: ["M6 6l12 12", "M18 6L6 18"],
    moon: ["M20 15.4A8 8 0 1 1 8.6 4 6.5 6.5 0 0 0 20 15.4Z"],
    sun: ["M12 3v2", "M12 19v2", "M3 12h2", "M19 12h2", "M5.6 5.6 7 7", "M17 17l1.4 1.4", "M18.4 5.6 17 7", "M7 17l-1.4 1.4", "M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z"],
    settings: ["M12 8.5a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7Z", "M19 13.5v-3l-2-.7a7 7 0 0 0-.8-1.8l.9-1.9L15 4l-1.9.9a7 7 0 0 0-2.2 0L9 4 6.9 6.1 7.8 8A7 7 0 0 0 7 9.8l-2 .7v3l2 .7a7 7 0 0 0 .8 1.8l-.9 1.9L9 20l1.9-.9a7 7 0 0 0 2.2 0l1.9.9 2.1-2.1-.9-1.9a7 7 0 0 0 .8-1.8Z"],
    file: ["M7 3h7l4 4v14H7Z", "M14 3v5h5", "M9 13h6", "M9 17h6"],
  };
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  (paths[name] || paths.file).forEach((data) => {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", data);
    svg.append(path);
  });
  return svg;
}

export function brandMark() {
  return element("div", { className: "brand-mark", "aria-hidden": "true" }, [element("span"), element("span"), element("span")]);
}

export function badge(label, tone = "") {
  return element("span", { className: `badge ${tone ? `badge--${tone}` : ""}`, text: label });
}

export function button(label, options = {}) {
  const node = element(options.href ? "a" : "button", {
    className: `button button--${options.kind || "secondary"} ${options.block ? "button--block" : ""}`,
    text: label,
    href: options.href,
    type: options.href ? undefined : (options.type || "button"),
    disabled: options.disabled,
    title: options.title,
    "data-mutates": options.mutates ? "true" : null,
    "data-allow-completed": options.allowCompleted ? "true" : null,
    "data-requires-runtime": options.requiresRuntime ? "true" : null,
    onClick: options.onClick,
  });
  if (options.disabled && options.reason) node.setAttribute("aria-description", options.reason);
  return node;
}

export function iconButton(name, label, onClick, extraClass = "") {
  return element("button", { className: `icon-button ${extraClass}`, type: "button", "aria-label": label, title: label, onClick }, icon(name));
}

export function showToast(message) {
  const region = document.getElementById("toast-region");
  const toast = element("div", { className: "toast", text: message, role: "status" });
  region.append(toast);
  window.setTimeout(() => toast.remove(), 4500);
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("ppt-agent:theme", theme);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#1b1626" : "#7c3aed");
}

export function preferredTheme() {
  return localStorage.getItem("ppt-agent:theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
