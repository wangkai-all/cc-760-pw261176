import { api, h, mount, toast, errorToast, num, emptyState } from "../ui.js";
import { store, go, refreshAll, replan } from "../app.js";
import { ternaryPlot, compositionBars, barChart } from "../charts.js";
import { runTable, statusLabel } from "./overview.js";

async function createTask() {
  const name = window.prompt("任务名称", `第 ${store.tasks.length + 1} 批试验`);
  if (!name) return;
  try {
    const res = await api.post(`/api/studies/${store.studyId}/tasks`, { name });
    toast(res.plan.partial
      ? `已创建，但只能满足 ${res.plan.feasible_size}/${res.plan.requested_size} 次试验`
      : `已创建任务，推荐 ${res.plan.feasible_size} 次试验`,
      res.plan.partial ? "bad" : "good", 6000);
    await refreshAll();
    go("tasks", res.id);
  } catch (err) {
    errorToast(err);
  }
}

async function updateDesign(fieldName, value) {
  if (!store.task) return;
  const design = { ...store.task.design, [fieldName]: value };
  try {
    await api.post(`/api/studies/${store.studyId}/tasks/${store.taskId}/plan`, { design });
    await refreshAll();
    toast("已按新设计参数重新规划", "good");
  } catch (err) {
    errorToast(err);
  }
}

function blockingList(blocking) {
  if (!blocking || !blocking.length) return null;
  return h("div", { class: "notes" },
    ...blocking.map((b) =>
      h("div", { class: `note-item ${b.kind === "level_coverage" ? "warn" : "bad"}` },
        h("div", { class: "title" }, kindLabel(b.kind)),
        h("div", { class: "small secondary" }, b.message || ""),
        b.shortfall !== undefined ? h("div", { class: "small muted" }, `需要 ${num(b.required, 2)}，可用 ${num(b.available, 2)}，缺 ${num(b.shortfall, 2)}`) : null,
        b.violations ? h("ul", {}, ...b.violations.map((v) => h("li", {}, v.message))) : null,
      ),
    ),
  );
}

function kindLabel(kind) {
  return {
    stock: "库存不足",
    level_coverage: "档位覆盖不足",
    forbidden_pair: "违反禁配",
    forbidden_group: "违反互斥组",
    bounds: "超出上下限",
    sum: "总和不符",
    candidates: "无可用候选",
    batch_size: "批量未满足",
  }[kind] || kind;
}

function metricsCard(batch) {
  if (!batch) return null;
  const m = batch.metrics || {};
  const terms = (store.diagnostics?.terms || []);
  const top = (batch.runs || []).filter((r) => r.status !== "cancelled")
    .map((r) => ({ label: `#${r.ordinal} ${r.level || ""}`.trim(), value: r.total_info }));
  return h("div", { class: "grid two" },
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "设计指标"), h("span", { class: "badge info" }, m.search || batch.search)),
      h("dl", { class: "kv" },
        h("dt", {}, "联合 D 准则 Φ(D)"), h("dd", {}, num(m.criterion, 2)),
        h("dt", {}, "混合项 Φ"), h("dd", {}, num(m.mixture_criterion, 2)),
        h("dt", {}, "设计秩 / 参数"), h("dd", {}, `${m.rank ?? "—"} / ${m.n_terms ?? "—"}`),
        h("dt", {}, "条件数"), h("dd", {}, num(m.condition, 2)),
        h("dt", {}, "平均杠杆"), h("dd", {}, num(m.mean_leverage, 2)),
        h("dt", {}, "重复点"), h("dd", {}, (m.duplicates || []).length ? m.duplicates.map((d) => `#${d}`).join("、") : "无"),
        h("dt", {}, "档位分布"), h("dd", {}, (m.levels || []).filter(Boolean).join(" · ") || "—"),
      ),
      h("p", { class: "small muted", style: { "margin-top": "8px" } },
        "同一准则同时判断「换配方」与「换加工档位」：每个候选方案都整批重算 Φ，不按单点贪心累加。"),
      h("details", { class: "table-view" },
        h("summary", {}, "不可辨识参数"),
        h("p", { class: "small secondary" }, ((m.aliased_terms || []).join("、")) || "本批后全部可辨识")),
    ),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "单次试验的信息增益"), h("span", { class: "hint" }, "按批次顺序累加 Φ")),
      top.length ? barChart({ items: top, valueLabel: "Φ 增量" }) : emptyState("无试验"),
    ),
  );
}

function designControls() {
  const t = store.task;
  if (!t) return null;
  const d = t.design;
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "设计参数"), h("span", { class: "hint" }, "修改后立即重新规划；上一版待投料试验会被取消")),
    h("div", { class: "form-row" },
      h("div", { class: "field" }, h("label", {}, "批量"), h("input", { type: "number", min: "1", value: d.batch_size, onchange: (e) => updateDesign("batch_size", Number(e.target.value)) })),
      h("div", { class: "field" }, h("label", {}, "每档最少次数"), h("input", { type: "number", min: "1", value: d.min_per_level, onchange: (e) => updateDesign("min_per_level", Number(e.target.value)) })),
      h("div", { class: "field" }, h("label", {}, "模型"), h("select", { onchange: (e) => updateDesign("model", e.target.value) },
        h("option", { value: "quadratic", selected: d.model === "quadratic" }, "二次"),
        h("option", { value: "linear", selected: d.model === "linear" }, "线性"))),
      h("div", { class: "field" }, h("label", {}, "加工档位"), h("select", { onchange: (e) => updateDesign("include_process", e.target.value === "1") },
        h("option", { value: "1", selected: !!d.include_process }, "纳入"),
        h("option", { value: "0", selected: !d.include_process }, "忽略"))),
      h("div", { class: "field" }, h("label", {}, "重复点"), h("select", { onchange: (e) => updateDesign("allow_replicates", e.target.value === "1") },
        h("option", { value: "1", selected: !!d.allow_replicates }, "允许"),
        h("option", { value: "0", selected: !d.allow_replicates }, "不允许"))),
    ),
    h("div", { class: "inline", style: { "margin-top": "12px" } },
      h("button", { class: "primary", onclick: () => replan() }, "按当前库存重新规划"),
      h("button", { onclick: () => go("inventory") }, "查看台账"),
    ),
  );
}

function revisionsCard() {
  const t = store.task;
  if (!t) return null;
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "版本历史"), h("span", { class: "hint" }, "每次规划生成一版；旧版未投料试验自动取消")),
    h("div", { class: "table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {},
          h("th", {}, "版本"), h("th", {}, "时间"), h("th", {}, "请求/可行"), h("th", {}, "搜索"), h("th", {}, "Φ"), h("th", {}, "状态"))),
        h("tbody", {}, ...t.batches.map((b) => {
          const counts = {};
          for (const r of b.runs) counts[r.status] = (counts[r.status] || 0) + 1;
          return h("tr", {},
            h("td", {}, `v${b.revision}`),
            h("td", { class: "small muted" }, b.created_at.replace("T", " ").slice(0, 16)),
            h("td", { class: "num" }, `${b.feasible_size}/${b.requested_size}`),
            h("td", { class: "small" }, b.search),
            h("td", { class: "num" }, num(b.metrics.criterion, 1)),
            h("td", { class: "small" }, Object.entries(counts).map(([k, v]) => `${statusLabel(k)} ${v}`).join(" · ")),
          );
        })),
      ),
    ),
  );
}

export function tasksView(root) {
  if (!store.tasks.length) {
    mount(root, h("div", { class: "card" },
      h("h2", {}, "还没有设计任务"),
      h("p", { class: "secondary" }, "创建任务后，系统会在约束区域内联合选择一批配方，并给出每个方案的信息增益。"),
      h("button", { class: "primary", style: { "margin-top": "12px" }, onclick: createTask }, "新建设计任务"),
    ));
    return;
  }
  const t = store.task;
  const batch = t ? t.batches[0] : null;
  const runs = batch ? batch.runs : [];
  const cfg = store.study.config;
  const codes = cfg.components.map((c) => c.code);
  const names = Object.fromEntries(cfg.components.map((c) => [c.code, c.name]));

  mount(root,
    h("div", { class: "stack" },
      h("div", { class: "card" },
        h("div", { class: "card-head" },
          h("div", { class: "inline" },
            h("h2", {}, "任务"),
            h("select", { onchange: (e) => go("tasks", Number(e.target.value)) },
              ...store.tasks.map((task) => h("option", { value: task.id, selected: task.id === store.taskId }, task.name))),
          ),
          h("div", { class: "inline" },
            h("button", { onclick: () => go("overview") }, "概览"),
            h("button", { class: "primary", onclick: createTask }, "新建任务")),
        ),
        t ? h("p", { class: "small muted" }, `任务 ${t.name} · 共 ${t.batches.length} 个版本`) : null,
        batch && batch.partial
          ? h("div", { class: "note-item bad", style: { "margin-top": "8px" } },
              h("div", { class: "title" }, `无法满足满批：可行 ${batch.feasible_size}/${batch.requested_size}`),
              blockingList(batch.blocking))
          : null,
      ),
      designControls(),
      h("div", { class: "card" },
        h("div", { class: "card-head" },
          h("h2", {}, "本批配方"),
          h("div", { class: "inline" },
            h("span", { class: "badge" }, `待投料 ${runs.filter((r) => r.status === "planned").length}`),
            h("span", { class: "badge" }, `已投料 ${runs.filter((r) => r.status === "dosed").length}`),
            h("span", { class: "badge good" }, `已完成 ${runs.filter((r) => r.status === "completed").length}`))),
        runTable(runs, { emptyText: "该版本没有可行试验" }),
      ),
      h("div", { class: "grid two" },
        h("div", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "组成分布")),
          compositionBars({ runs: runs.filter((r) => r.status !== "cancelled"), codes, names, total: cfg.region.total }),
        ),
        h("div", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "在区域中的位置")),
          ternaryPlot({
            codes,
            total: cfg.region.total,
            regionPoints: store.region ? store.region.points : [],
            layers: [{
              name: "本批",
              color: "var(--series-2)",
              points: runs.filter((r) => r.status !== "cancelled").map((r) => ({
                amounts: r.amounts,
                label: `#${r.ordinal} · ${r.level || ""}`,
                radius: r.locked ? 6 : 4.6,
                meta: [["信息增益", num(r.total_info, 2)], ["锁定", r.locked ? "是" : "否"]],
              })),
            }],
          }),
        ),
      ),
      metricsCard(batch),
      revisionsCard(),
    ),
  );
}
