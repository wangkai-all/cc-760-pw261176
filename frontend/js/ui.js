/* Shared helpers: API client, DOM builder, formatters, toasts, modal. */

export const api = {
  async request(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { error: text };
    }
    if (!res.ok) {
      const msg =
        (data && (data.error || (data.detail && JSON.stringify(data.detail)))) ||
        `请求失败 (${res.status})`;
      const err = new Error(typeof msg === "string" ? msg : "请求失败");
      err.status = res.status;
      err.detail = data && data.detail;
      throw err;
    }
    return data;
  },
  get(p) { return this.request("GET", p); },
  post(p, b) { return this.request("POST", p, b === undefined ? {} : b); },
  put(p, b) { return this.request("PUT", p, b); },
  del(p) { return this.request("DELETE", p); },
};

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(6)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function mount(el, ...children) {
  clear(el);
  append(el, children);
  return el;
}

const nf = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });

export function num(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (Math.abs(v) >= 10000) return nf0.format(v);
  return Number(v).toLocaleString("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

export function pct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function compact(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(a >= 1e4 ? 0 : 1)}K`;
  return num(v, a < 10 ? 2 : 1);
}

export function fmtAmounts(amounts, codes) {
  return (codes || Object.keys(amounts))
    .map((c) => `${c}=${num(amounts[c], 1)}`)
    .join("  ");
}

export const toastHost = h("div", { class: "toast-host", role: "status", "aria-live": "polite" });

export function toast(message, kind = "", timeout = 4200) {
  const el = h(
    "div",
    { class: `toast ${kind}` },
    h("span", { class: "x", onclick: () => el.remove(), role: "button", tabindex: "0" }, "✕"),
    message,
  );
  toastHost.appendChild(el);
  if (timeout) setTimeout(() => el.remove(), timeout);
  return el;
}

export function errorToast(err) {
  const msg = err && err.message ? err.message : String(err);
  const node = toast(h("div", { html: `<b>操作失败</b><div>${escapeHtml(msg)}</div>` }), "bad", 8000);
  return node;
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

export function confirmDialog({ title, body, okLabel = "确认", cancelLabel = "取消", danger = false }) {
  return new Promise((resolve) => {
    const close = (val) => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === "Escape") close(false);
      if (e.key === "Enter") close(true);
    };
    const backdrop = h(
      "div",
      { class: "modal-backdrop", onclick: (e) => { if (e.target === backdrop) close(false); } },
      h(
        "div",
        { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
        h("h2", {}, title),
        h("div", { class: "body" }, body),
        h(
          "div",
          { class: "actions" },
          h("button", { onclick: () => close(false) }, cancelLabel),
          h("button", { class: danger ? "danger" : "primary", onclick: () => close(true) }, okLabel),
        ),
      ),
    );
    document.body.appendChild(backdrop);
    document.addEventListener("keydown", onKey);
  });
}

export function emptyState(text) {
  return h("div", { class: "empty" }, text);
}

export function statusBadge(status) {
  const map = {
    planned: ["待投料", "info"],
    dosed: ["已投料", "warn"],
    completed: ["已完成", "good"],
    failed: ["失败", ""],
    cancelled: ["已取消", ""],
  };
  const [label, cls] = map[status] || [status, ""];
  return h("span", { class: `badge ${cls}` }, h("span", { class: `dot` }), label);
}

export function debounce(fn, ms = 250) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}
