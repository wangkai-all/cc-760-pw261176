import { h, mount, num, pct } from "../ui.js";
import { store, go } from "../app.js";
import { termSdChart, scatter } from "../charts.js";

function statRow(d) {
  const before = d.before;
  const after = d.after;
  const gain = after.rank - before.rank;
  const fmtVar = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
  return h("div", { class: "stat-row" },
    h("div", { class: "stat" },
      h("div", { class: "label" }, "可辨识参数（本批前）"),
      h("div", { class: "value" }, `${before.rank}`),
      h("div", { class: "delta" }, `共 ${before.n_terms} 个参数`)),
    h("div", { class: "stat" },
      h("div", { class: "label" }, "可辨识参数（本批后）"),
      h("div", { class: "value" }, `${after.rank}`),
      h("div", { class: "delta good" }, gain > 0 ? `+${gain} 个参数变得可辨识` : "无新增（本批已饱和）")),
    h("div", { class: "stat" },
      h("div", { class: "label" }, "预测方差 / 先验（本批前）"),
      h("div", { class: "value" }, fmtVar(d.mean_variance_before)),
      h("div", { class: "delta" }, "可行区域平均，1=完全未知")),
    h("div", { class: "stat" },
      h("div", { class: "label" }, "预测方差 / 先验（本批后）"),
      h("div", { class: "value" }, fmtVar(d.mean_variance_after)),
      h("div", { class: "delta" }, d.mean_variance_before && d.mean_variance_after
        ? `${pct(d.mean_variance_after / d.mean_variance_before)} 于本批前` : "—")),
  );
}

function fitCard(d) {
  const fit = d.fitted;
  const terms = d.terms;
  return h("div", { class: "grid side" },
    h("div", { class: "card" },
      h("div", { class: "card-head" },
        h("h2", {}, "参数后验 SD / 先验：追加前 → 追加后"),
        h("span", { class: "hint" }, "1.00 = 仍是先验；0 = 已被数据约束")),
      termSdChart({ terms }),
      h("p", { class: "small muted" },
        "柱长为「后验 SD ÷ 先验 SD」，越接近 0 表示该方向被数据约束得越紧；可辨识与否按未正则化秩判断，两列「是/否」即该判断结果。"),
    ),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "待投料试验")),
      d.open_runs.length
        ? h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "配方"), h("th", {}, "档位"), h("th", {}, "状态"))),
            h("tbody", {}, ...d.open_runs.map((r) =>
              h("tr", {},
                h("td", {}, `#${r.ordinal}`),
                h("td", { class: "small mono" }, Object.entries(r.amounts).map(([k, v]) => `${k} ${num(v, 1)}`).join("  ")),
                h("td", {}, r.level || "—"),
                h("td", {}, r.kind === "planned" ? h("span", { class: "badge info" }, "待投料") : h("span", { class: "badge warn" }, "已投料/待响应")),
              ))),
          ))
        : h("div", { class: "empty" }, "本批没有待投料试验"),
      h("div", { class: "inline", style: { "margin-top": "12px" } }, h("button", { onclick: () => go("tasks") }, "管理试验")),
    ),
  );
}

function flags(d) {
  const items = [];
  for (const s of d.structural) {
    items.push(h("div", { class: "note-item warn" }, h("div", { class: "title" }, `结构性不可辨识：${s.component}`), h("p", { class: "small secondary" }, s.message)));
  }
  if (d.excluded.length) {
    items.push(h("div", { class: "note-item warn" },
      h("div", { class: "title" }, "未参与拟合的历史配方"),
      h("ul", {}, ...d.excluded.map((e) => h("li", {}, `${e.ref}：${e.reason}`)))));
  }
  if (d.failed.length) {
    items.push(h("div", { class: "note-item" },
      h("div", { class: "title" }, "失败 / 无响应试验"),
      h("ul", {}, ...d.failed.map((f) => h("li", {}, `${f.ref} · ${f.source === "history" ? "历史" : "试验"}：${f.note}`)))));
  }
  const aliased = d.terms.filter((t) => !t.identifiable_after);
  if (aliased.length) {
    items.push(h("div", { class: "note-item info" },
      h("div", { class: "title" }, `本批后仍不可辨识：${aliased.map((t) => t.term).join("、")}`),
      h("p", { class: "small secondary" }, "这些方向在当前区域或库存下无法被进一步约束；继续加批次收益很小。")));
  }
  if (!items.length) items.push(h("div", { class: "note-item good" }, h("div", { class: "title" }, "全部参数可辨识")));
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "诊断说明")), h("div", { class: "notes" }, ...items));
}

function fmtSigma(v) {
  if (v == null) return "—";
  if (v > 0 && v < 0.01) return v.toExponential(1);
  return num(v, 3);
}

function coefficientTable(d) {
  const fit = d.fitted;
  // Report the SD relative to the prior (1.0 = the data added no information).
  // The absolute posterior SD is sigma-scaled and would read as a huge number
  // for aliased directions even though those directions are simply unknown.
  const rows = d.terms.map((t) => {
    const coef = fit.coefficients[t.term];
    const relative = t.after_sd;
    const alias = !t.identifiable_after;
    return h("tr", { class: alias ? "is-aliased" : "" },
      h("td", {}, t.term),
      h("td", { class: "num" }, alias ? h("span", { class: "muted" }, "—") : num(coef, 3)),
      h("td", { class: "num" }, relative == null ? "—" : alias ? h("span", { class: "muted" }, `${num(relative, 3)}（未辨识）`) : num(relative, 3)),
      h("td", {}, t.identifiable_before ? "是" : "否"),
      h("td", {}, alias ? h("span", { class: "muted" }, "否") : "是"),
    );
  });
  return h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("h2", {}, "回归系数"),
      h("span", { class: "hint" }, `参考编码：其余成分以基准成分作差 · 观测 ${fit.n_obs} 次，残差方差 σ̂² = ${fmtSigma(fit.sigma2)}`)),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "参数"), h("th", { class: "num" }, "估计值"), h("th", { class: "num" }, "后验 SD / 先验"), h("th", {}, "追加前"), h("th", {}, "追加后"))),
      h("tbody", {}, ...rows),
    )),
    h("p", { class: "small muted" }, "后验 SD 以先验标准差为 1 归一化（岭先验，故最大值仍为 1）；越接近 0 表示该参数被数据约束得越紧。标「未辨识」的项在未正则化意义下秩亏，估计值不显示。"),
  );
}

function historyScatter(d) {
  const cfg = store.study.config;
  const codes = cfg.components.map((c) => c.code);
  const colorOf = (level) => {
    const idx = cfg.process_levels.indexOf(level);
    return `var(--series-${(idx >= 0 ? idx : 0) % 8 + 1})`;
  };
  const layers = cfg.process_levels.map((level) => ({
    name: `档位 ${level}`,
    color: colorOf(level),
    points: d.history.filter((o) => o.level === level).map((o) => ({
      x: (o.amounts[codes[0]] || 0) / cfg.region.total,
      y: o.response,
      label: `${o.ref} · ${level}`,
      meta: [["响应", num(o.response, 2)], ...codes.map((c) => [c, num(o.amounts[c], 1)])],
    })),
  }));
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "响应与主成分关系"), h("span", { class: "hint" }, `${cfg.response_name} 对 ${codes[0]} 比例`)),
    d.history.length
      ? scatter({ layers, xLabel: `${codes[0]} 质量分数`, yLabel: cfg.response_name, xFormat: (v) => pct(v, 0) })
      : h("div", { class: "empty" }, "暂无可用观测"),
  );
}

export function diagnosticsView(root) {
  const d = store.diagnostics;
  if (!d) { mount(root, h("div", { class: "card" }, "尚未计算诊断")); return; }
  mount(root,
    h("div", { class: "stack" },
      statRow(d),
      fitCard(d),
      h("div", { class: "grid two" }, historyScatter(d), flags(d)),
      coefficientTable(d),
    ),
  );
}
