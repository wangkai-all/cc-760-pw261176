import { h, mount, errorToast, num, statusBadge } from "../ui.js";
import { store, go, doseRun, cancelRun, recordResponse, failRun, toggleLock, replan } from "../app.js";
import { ternaryPlot, compositionBars } from "../charts.js";

export function runTable(runs, opts = {}) {
  if (!runs.length) {
    return h("div", { class: "empty" }, opts.emptyText || "暂无试验");
  }
  const codes = Object.keys(runs[0].amounts);
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { class: "run-table" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", {}, "#"),
          h("th", {}, "配方"),
          h("th", {}, "档位"),
          h("th", { class: "num" }, "信息增益"),
          h("th", {}, "投料量"),
          h("th", {}, "状态"),
          h("th", {}, "操作"),
        ),
      ),
      h(
        "tbody",
        {},
        ...runs.map((run) => {
          const status = run.status || "planned";
          return h(
            "tr",
            { class: `is-${status}` },
            h("td", {}, h("strong", {}, `#${run.ordinal}`), run.duplicate ? h("span", { class: "badge warn", style: { marginLeft: "6px" } }, "重复点") : null, run.locked ? h("span", { class: "badge info", style: { marginLeft: "6px" } }, "已锁定") : null),
            h("td", { class: "mono small" }, codes.map((c) => `${c} ${num(run.amounts[c], 1)}`).join("  ")),
            h("td", {}, run.level || h("span", { class: "muted" }, "—")),
            h(
              "td",
              { class: "num" },
              h("span", { title: "该次试验为整批 D 准则带来的增量（含档位项）" }, num(run.total_info, 2)),
            ),
            h("td", { class: "small mono" }, codes.filter((c) => (run.usage[c] || 0) > 0).map((c) => `${c} ${num(run.usage[c], 1)}`).join("  ") || h("span", { class: "muted" }, "—")),
            h("td", {}, statusBadge(status)),
            h(
              "td",
              { class: "actions" },
              ...actionsFor(run, opts.editable !== false),
            ),
          );
        }),
      ),
    ),
  );
}

function actionsFor(run, editable) {
  if (!editable) return [];
  const out = [];
  const label = `#${run.ordinal}`;
  if (run.status === "planned") {
    out.push(h("button", { class: "small", onclick: () => doseRun(run.id, label), title: "确认投料并扣减库存" }, "确认投料"));
    out.push(h("button", { class: "small", onclick: () => toggleLock(run), title: "锁定后重新规划会保留该配方" }, run.locked ? "解锁" : "锁定"));
    out.push(h("button", { class: "small danger", onclick: () => cancelRun(run.id, label), title: "取消并释放原料" }, "取消"));
  } else if (run.status === "dosed") {
    out.push(h("button", { class: "small primary", onclick: () => recordResponse(run), title: "录入本轮合成响应" }, "录入响应"));
    out.push(h("button", { class: "small danger", onclick: () => failRun(run), title: "标记失败，不计入拟合" }, "标记失败"));
  } else if (run.status === "completed") {
    out.push(h("button", { class: "small", onclick: () => recordResponse(run), title: "修正响应值" }, "修改响应"));
  }
  return out;
}

export function statusSummary(batch) {
  if (!batch) return h("span", { class: "muted" }, "—");
  const counts = {};
  for (const r of batch.runs) counts[r.status] = (counts[r.status] || 0) + 1;
  return h(
    "div",
    { class: "chips" },
    ...Object.entries(counts).map(([k, v]) => h("span", { class: "badge" }, `${statusLabel(k)} ${v}`)),
  );
}

function statusLabel(s) {
  return { planned: "待投料", dosed: "已投料", completed: "已完成", failed: "失败", cancelled: "已取消" }[s] || s;
}

function overviewNumbers() {
  const inv = store.inventory.inventory;
  const diag = store.diagnostics;
  const nHist = diag ? diag.history.length : 0;
  const rankBefore = diag ? diag.before.rank : 0;
  const rankAfter = diag ? diag.after.rank : 0;
  const nTerms = diag ? diag.before.n_terms : 0;
  return h(
    "div",
    { class: "stat-row" },
    h("div", { class: "stat" },
      h("div", { class: "label" }, "可用历史观测"),
      h("div", { class: "value" }, num(nHist)),
      h("div", { class: "delta" }, `${diag ? diag.failed.length : 0} 次失败/无响应已排除`)),
    h("div", { class: "stat" },
      h("div", { class: "label" }, "可辨识参数（追加前 → 追加后）"),
      h("div", { class: "value" }, `${rankBefore} → ${rankAfter}`),
      h("div", { class: "delta" }, `模型共 ${nTerms} 个参数`)),
    h("div", { class: "stat" },
      h("div", { class: "label" }, "下一批试验数"),
      h("div", { class: "value" }, store.task ? num(store.task.batches[0]?.feasible_size ?? 0) : "—"),
      h("div", { class: "delta" }, store.task && store.task.batches[0]?.partial ? h("span", { class: "badge bad" }, "未满批") : "满批可行")),
    h("div", { class: "stat" },
      h("div", { class: "label" }, "最紧张原料可用量"),
      h("div", { class: "value" }, num(Math.min(...inv.map((i) => Math.max(i.available, 0))), 0)),
      h("div", { class: "delta" }, h("span", { class: "muted" }, "单位 " + (inv[0]?.unit || "g")))),
  );
}

function nextBatchCard() {
  if (!store.task) {
    return h("div", { class: "card" }, h("h2", {}, "下一批"),
      h("p", { class: "secondary" }, "还没有设计任务。"),
      h("button", { class: "primary", style: { marginTop: "10px" }, onclick: () => go("tasks") }, "创建设计任务"));
  }
  const batch = store.task.batches[0];
  const runs = (batch ? batch.runs : []).filter((r) => r.status !== "cancelled");
  const codes = store.study.config.components.map((c) => c.code);
  const names = Object.fromEntries(store.study.config.components.map((c) => [c.code, c.name]));
  const total = store.study.config.region.total;
  return h(
    "div",
    { class: "card" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "下一批推荐配方"), h("div", { class: "hint" }, `${store.task.name} · 第 ${batch?.revision ?? 0} 版 · ${batch?.search || ""}`)),
      h("div", { class: "inline" },
        h("button", { onclick: () => go("tasks", store.task.id) }, "全部试验"),
        h("button", { class: "primary", onclick: () => replan().catch(errorToast) }, "重新规划")),
    ),
    batch?.partial
      ? h("div", { class: "note-item bad" },
          h("div", { class: "title" }, `只能满足 ${batch.feasible_size}/${batch.requested_size} 次试验`),
          h("ul", {}, ...(batch.blocking || []).slice(0, 4).map((b) => h("li", {}, b.message))))
      : null,
    h("div", { class: "grid two", style: { "align-items": "start", "margin-top": "8px" } },
      h("div", {},
        ternaryPlot({
          codes,
          total,
          regionPoints: store.region ? store.region.points : [],
          layers: [
            {
              name: "本批配方",
              color: "var(--series-2)",
              points: runs.map((r) => ({
                amounts: r.amounts,
                label: `#${r.ordinal} · ${r.level || ""}`,
                meta: [["信息增益", num(r.total_info, 2)], ["状态", statusLabel(r.status)]],
              })),
            },
            {
              name: "历史配方",
              color: "var(--series-1)",
              hollow: true,
              points: (store.diagnostics ? store.diagnostics.history : []).map((o) => ({
                amounts: o.amounts,
                label: o.ref,
                meta: [["响应", num(o.response, 2)], ["档位", o.level || "—"]],
              })),
            },
          ],
          lower: Object.fromEntries(store.study.config.components.map((c) => [c.code, c.lower])),
          upper: Object.fromEntries(store.study.config.components.map((c) => [c.code, c.upper])),
        }),
        h("p", { class: "small muted" }, "空心圈=历史配方，实心点=本批推荐；灰色小点=可行格点。三元顶点为 A、B 与其余 C+D+E 合计。"),
      ),
      h("div", { class: "stack" },
        compositionBars({ runs, codes, names, total }),
        h("p", { class: "small muted" }, `每根横条为一次推荐配方，段宽按投料量占总量 ${num(total, 0)} 的比例；鼠标悬停可看精确投料量。`),
      ),
    ),
  );
}

function ledgerMini() {
  const inv = store.inventory.inventory;
  const max = Math.max(...inv.map((i) => i.balance), 1);
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "原料占用"), h("button", { class: "ghost small", onclick: () => go("inventory") }, "台账 →")),
    h("div", { class: "stack" },
      ...inv.map((row) =>
        h("div", {},
          h("div", { class: "inline", style: { "justify-content": "space-between", "margin-bottom": "3px" } },
            h("span", {}, h("strong", {}, row.code), " ", h("span", { class: "muted small" }, row.name)),
            h("span", { class: "small mono" }, `${num(row.available, 0)} 可用 / ${num(row.balance, 0)} ${row.unit}`)),
          h("div", { class: "bar-track", title: `在库 ${num(row.balance, 1)}，已占用 ${num(row.held, 1)}` },
            h("div", { class: "bar-fill", style: { width: `${Math.min(100, (row.available / max) * 100)}%` } }),
            h("div", { class: "bar-fill held", style: { width: `${Math.min(100, (row.held / max) * 100)}%` } }),
          ),
        ),
      ),
    ),
    h("div", { class: "legend-inline", style: { "margin-top": "10px" } },
      h("span", {}, h("i", { style: { background: "var(--series-1)" } }), "可用"),
      h("span", {}, h("i", { style: { background: "var(--series-4)" } }), "被待投料试验占用")),
  );
}

function flagsCard() {
  const d = store.diagnostics;
  if (!d) return null;
  const items = [];
  if (d.structural.length) {
    items.push(h("div", { class: "note-item warn" },
      h("div", { class: "title" }, "结构性不可辨识"),
      h("ul", {}, ...d.structural.map((s) => h("li", {}, s.message)))));
  }
  if (d.excluded.length) {
    items.push(h("div", { class: "note-item warn" },
      h("div", { class: "title" }, `${d.excluded.length} 条历史配方未参与拟合`),
      h("ul", {}, ...d.excluded.slice(0, 3).map((e) => h("li", {}, `${e.ref}：${e.reason}`)))));
  }
  if (d.failed.length) {
    items.push(h("div", { class: "note-item" },
      h("div", { class: "title" }, `${d.failed.length} 次失败/无响应`),
      h("ul", {}, ...d.failed.slice(0, 3).map((f) => h("li", {}, `${f.ref}：${f.note}`)))));
  }
  const aliased = d.terms.filter((t) => !t.identifiable_after);
  if (aliased.length) {
    items.push(h("div", { class: "note-item info" },
      h("div", { class: "title" }, `本批后仍有 ${aliased.length} 个参数不可辨识`),
      h("ul", {}, ...aliased.slice(0, 4).map((t) => h("li", {}, `${t.term}：后验 SD 为先验的 ${num(t.after_sd, 2)} 倍`)))));
  }
  if (!items.length) {
    items.push(h("div", { class: "note-item good" }, h("div", { class: "title" }, "没有异常"), h("p", { class: "small muted" }, "全部参数在本批后可辨识。")));
  }
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "需要留意"), h("button", { class: "ghost small", onclick: () => go("diagnostics") }, "辨识详情 →")), h("div", { class: "notes" }, ...items));
}

export function overviewView(root) {
  mount(
    root,
    h("div", { class: "stack" },
      overviewNumbers(),
      nextBatchCard(),
      h("div", { class: "grid two", style: { "align-items": "start" } },
        ledgerMini(),
        flagsCard() || h("div")),
    ),
  );
}

export { statusLabel };
