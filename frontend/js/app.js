import { api, h, mount, toast, toastHost, errorToast, confirmDialog, emptyState, num, statusBadge } from "./ui.js";
import { overviewView } from "./views/overview.js";
import { regionView } from "./views/region.js";
import { tasksView } from "./views/tasks.js";
import { inventoryView } from "./views/inventory.js";
import { diagnosticsView } from "./views/diagnostics.js";
import { historyView } from "./views/history.js";

export const store = {
  studyId: null,
  study: null,
  studies: [],
  view: "overview",
  taskId: null,
  task: null,
  diagnostics: null,
  inventory: null,
  region: null,
  history: null,
  tasks: [],
  loading: false,
};

const views = {
  overview: { title: "概览", ico: "◈", render: overviewView },
  region: { title: "试验区", ico: "△", render: regionView },
  tasks: { title: "设计任务", ico: "◎", render: tasksView },
  inventory: { title: "原料台账", ico: "▤", render: inventoryView },
  diagnostics: { title: "拟合与辨识", ico: "∿", render: diagnosticsView },
  history: { title: "历史数据", ico: "≡", render: historyView },
};

const main = document.getElementById("main");
const nav = document.getElementById("nav");
const topbar = document.getElementById("topbar");

/* ------------------------------------------------------------------ routing */

function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [view, arg] = hash.split("/");
  if (views[view]) store.view = view;
  if (arg) store.taskId = Number(arg);
  renderNav();
  render();
}

window.addEventListener("hashchange", route);

export function go(view, taskId) {
  location.hash = `#/${view}${taskId ? `/${taskId}` : ""}`;
}

function renderNav() {
  mount(
    nav,
    ...Object.entries(views).map(([key, v]) =>
      h(
        "button",
        {
          onclick: () => go(key),
          "aria-current": store.view === key ? "page" : null,
        },
        h("span", { class: "ico" }, v.ico),
        v.title,
      ),
    ),
  );
}

/* ------------------------------------------------------------------ data */

export async function loadStudies() {
  store.studies = await api.get("/api/studies");
  if (!store.studies.length) {
    store.studyId = null;
    return;
  }
  if (!store.studyId || !store.studies.some((s) => s.id === store.studyId)) {
    store.studyId = store.studies[0].id;
  }
}

export async function refresh({ diagnostics = true } = {}) {
  if (!store.studyId) return;
  const id = store.studyId;
  store.loading = true;
  try {
    const [study, inventory, tasks, region] = await Promise.all([
      api.get(`/api/studies/${id}`),
      api.get(`/api/studies/${id}/inventory`),
      api.get(`/api/studies/${id}/tasks`),
      api.get(`/api/studies/${id}/region`),
    ]);
    store.study = study;
    store.inventory = inventory;
    store.tasks = tasks;
    store.region = region;
    store.history = await api.get(`/api/studies/${id}/history`);
    if (!store.taskId && tasks.length) store.taskId = tasks[0].id;
    if (store.taskId && !tasks.some((t) => t.id === store.taskId)) {
      store.taskId = tasks.length ? tasks[0].id : null;
    }
    store.task = store.taskId
      ? await api.get(`/api/studies/${id}/tasks/${store.taskId}`)
      : null;
    if (diagnostics) {
      store.diagnostics = await api.get(`/api/studies/${id}/diagnostics`);
    }
  } finally {
    store.loading = false;
  }
}

export async function refreshRegion() {
  if (!store.studyId) return;
  store.region = await api.get(`/api/studies/${store.studyId}/region`);
}

export async function refreshAll() {
  await refresh();
  render();
}

/* ------------------------------------------------------------------ actions */

export async function doseRun(runId, label) {
  const ok = await confirmDialog({
    title: `确认投料 ${label || ""}`,
    body: h(
      "div",
      { class: "stack" },
      h("p", {}, "确认投料后，该试验占用的原料会从台账扣除（余额下降、占用消失），且不能再取消。"),
      h("p", { class: "small muted" }, "这一步不可撤销；若尚未投料，请改用「取消试验」释放原料。"),
    ),
    okLabel: "确认已投料",
  });
  if (!ok) return false;
  try {
    await api.post(`/api/studies/${store.studyId}/runs/${runId}/dose`);
    toast("投料已确认，原料已从台账扣除", "good");
    await refreshAll();
    return true;
  } catch (err) {
    errorToast(err);
    return false;
  }
}

export async function cancelRun(runId, label) {
  const ok = await confirmDialog({
    title: `取消试验 ${label || ""}`,
    body: h("p", {}, "取消尚未投料的试验会立即释放它占用的原料；已投料的试验不能取消。"),
    okLabel: "取消试验",
    danger: true,
  });
  if (!ok) return false;
  try {
    await api.post(`/api/studies/${store.studyId}/runs/${runId}/cancel`);
    toast("已取消，原料占用已释放", "good");
    await refreshAll();
    return true;
  } catch (err) {
    errorToast(err);
    return false;
  }
}

export async function toggleLock(run) {
  try {
    await api.post(`/api/studies/${store.studyId}/runs/${run.id}/lock`, { locked: !run.locked });
    await refreshAll();
    toast(run.locked ? "已解锁" : "已锁定，重新规划时保留该配方", "");
  } catch (err) {
    errorToast(err);
  }
}

export function responseDialog(run) {
  return new Promise((resolve) => {
    const input = h("input", {
      type: "number",
      step: "any",
      required: true,
      value: run.response ?? "",
      placeholder: "例如 42.6",
    });
    const note = h("input", { type: "text", placeholder: "可选备注" });
    const close = (val) => {
      backdrop.remove();
      resolve(val);
    };
    const submit = () => {
      const v = Number(input.value);
      if (!Number.isFinite(v) || input.value === "") {
        input.focus();
        input.setAttribute("aria-invalid", "true");
        return;
      }
      close({ value: v, note: note.value });
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    const backdrop = h(
      "div",
      { class: "modal-backdrop", onclick: (e) => { if (e.target === backdrop) close(null); } },
      h(
        "div",
        { class: "modal", role: "dialog", "aria-modal": "true" },
        h("h2", {}, `录入响应 · 试验 #${run.ordinal}`),
        h("p", { class: "small muted" }, `配方 ${Object.entries(run.amounts).map(([k, v]) => `${k}=${num(v, 1)}`).join("  ")} · 档位 ${run.level || "—"}`),
        h("div", { class: "body stack" },
          h("div", { class: "field" }, h("label", {}, store.study?.config?.response_name || "响应值"), input),
          h("div", { class: "field" }, h("label", {}, "备注"), note),
        ),
        h("div", { class: "actions" },
          h("button", { onclick: () => close(null) }, "取消"),
          h("button", { class: "primary", onclick: submit }, "保存响应"),
        ),
      ),
    );
    document.body.appendChild(backdrop);
    input.focus();
  });
}

export async function recordResponse(run) {
  const res = await responseDialog(run);
  if (!res) return;
  try {
    await api.post(`/api/studies/${store.studyId}/runs/${run.id}/response`, res);
    toast("响应已录入；可辨识参数已更新", "good");
    await refreshAll();
  } catch (err) {
    errorToast(err);
  }
}

export async function failRun(run) {
  const ok = await confirmDialog({
    title: `将试验 #${run.ordinal} 标记为失败`,
    body: h("p", {}, "原料已消耗不会退回；该试验不计入拟合，但会出现在失败试验清单中。"),
    okLabel: "标记失败",
    danger: true,
  });
  if (!ok) return;
  try {
    await api.post(`/api/studies/${store.studyId}/runs/${run.id}/fail`, { note: "人工标记失败" });
    toast("已标记失败（原料不退回）", "");
    await refreshAll();
  } catch (err) {
    errorToast(err);
  }
}

export async function replan() {
  if (!store.taskId) return;
  try {
    const res = await api.post(`/api/studies/${store.studyId}/tasks/${store.taskId}/plan`, {});
    if (res.partial) {
      toast(`只能生成 ${res.feasible_size}/${res.requested_size} 次试验，见阻碍约束`, "bad", 7000);
    } else {
      toast(`已重新规划 ${res.feasible_size} 次试验（第 ${res.revision} 版）`, "good");
    }
    await refreshAll();
  } catch (err) {
    errorToast(err);
  }
}

/* ------------------------------------------------------------------ render */

function render() {
  if (!store.studyId) {
    mount(
      topbar,
      h("div", {}, h("h1", {}, "配方试验设计"), h("div", { class: "sub" }, "约束混合试验 · 响应面拟合 · 下一代配方推荐")),
    );
    mount(
      main,
      h(
        "div",
        { class: "card" },
        h("h2", {}, "还没有研究"),
        h("p", { class: "secondary" }, "创建一个研究，或载入内置演示（含虚构历史配方、库存、失败试验与第一批设计）。"),
        h("div", { class: "inline", style: { marginTop: "14px" } },
          h("button", { class: "primary", onclick: createDemo }, "载入演示数据"),
          h("button", { onclick: createStudy }, "新建空白研究"),
        ),
      ),
    );
    return;
  }

  const def = views[store.view];
  const studyName = store.study?.study?.name || "研究";
  mount(
    topbar,
    h("div", {}, h("h1", {}, def.title), h("div", { class: "sub" }, `${studyName} · ${store.study?.config?.response_name || "响应"}`)),
    h(
      "div",
      { class: "topbar-actions" },
      h("button", { onclick: () => go("region"), title: "编辑试验区与约束" }, "试验区设置"),
      h("button", { onclick: () => refreshAll().catch(errorToast), title: "重新读取服务端状态" }, "刷新"),
    ),
  );
  const body = h("div", {});
  mount(main, body);
  try {
    def.render(body);
  } catch (err) {
    console.error(err);
    mount(body, h("div", { class: "card" }, h("h2", {}, "渲染失败"), h("pre", { class: "small" }, String(err && err.stack || err))));
  }
}

async function createDemo() {
  try {
    const res = await api.post("/api/demo");
    await loadStudies();
    store.studyId = res.study_id;
    await refreshAll();
    go("overview");
    toast("演示数据已就绪", "good");
  } catch (err) {
    errorToast(err);
  }
}

async function createStudy() {
  try {
    const res = await api.post("/api/studies", { name: "新研究" });
    await loadStudies();
    store.studyId = res.id;
    store.taskId = null;
    await refreshAll();
    go("region");
    toast("已创建研究，请设置试验区", "good");
  } catch (err) {
    errorToast(err);
  }
}

/* ------------------------------------------------------------------ boot */

(async function boot() {
  document.body.appendChild(toastHost);
  try {
    await loadStudies();
    if (store.studyId) await refresh();
  } catch (err) {
    errorToast(err);
  }
  route();
})();

export { emptyState, statusBadge, api, h, mount, toast, errorToast, num };
