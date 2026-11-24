import { api, h, mount, toast, errorToast, num } from "../ui.js";
import { store, refreshAll, refreshRegion } from "../app.js";
import { ternaryPlot } from "../charts.js";

let draft = null;

function ensureDraft() {
  if (draft && draft._studyId === store.studyId) return draft;
  const cfg = store.study.config;
  draft = {
    _studyId: store.studyId,
    total: cfg.region.total,
    n_steps: cfg.region.n_steps,
    components: cfg.components.map((c) => ({ ...c })),
    forbidden_pairs: cfg.region.forbidden_pairs.map((p) => [...p]),
    forbidden_groups: cfg.region.forbidden_groups.map((g) => [...g]),
    process_levels: [...cfg.process_levels],
    min_per_level: cfg.min_per_level,
    model: cfg.model,
    include_process: cfg.include_process,
    allow_replicates: cfg.allow_replicates,
    batch_size: cfg.batch_size,
    charge_mass: cfg.charge_mass,
    stock: { ...cfg.stock },
    response_name: cfg.response_name,
  };
  return draft;
}

function resetDraft() {
  draft = null;
}

async function save() {
  draft = ensureDraft();
  const payload = {
    components: draft.components,
    region: {
      total: Number(draft.total),
      n_steps: Number(draft.n_steps),
      forbidden_pairs: draft.forbidden_pairs,
      forbidden_groups: draft.forbidden_groups,
    },
    process_levels: draft.process_levels,
    min_per_level: Number(draft.min_per_level),
    model: draft.model,
    include_process: draft.include_process,
    allow_replicates: draft.allow_replicates,
    batch_size: Number(draft.batch_size),
    charge_mass: Number(draft.charge_mass),
    stock: Object.fromEntries(Object.entries(draft.stock).map(([k, v]) => [k, Number(v)])),
    response_name: draft.response_name,
  };
  try {
    await api.put(`/api/studies/${store.studyId}/config`, payload);
    toast("试验区已保存", "good");
    resetDraft();
    await refreshAll();
    await refreshRegion();
  } catch (err) {
    errorToast(err);
  }
}

function componentEditor() {
  const d = ensureDraft();
  const rows = d.components.map((c, idx) =>
    h("tr", {},
      h("td", {}, h("input", { value: c.code, style: { width: "62px" }, oninput: (e) => { c.code = e.target.value.trim(); } })),
      h("td", {}, h("input", { value: c.name, style: { width: "120px" }, oninput: (e) => { c.name = e.target.value; } })),
      h("td", {}, h("input", { type: "number", step: "any", value: c.lower, style: { width: "86px" }, oninput: (e) => { c.lower = e.target.value; } })),
      h("td", {}, h("input", { type: "number", step: "any", value: c.upper, style: { width: "86px" }, oninput: (e) => { c.upper = e.target.value; } })),
      h("td", {}, h("input", { type: "number", step: "any", value: d.stock[c.code] ?? 0, style: { width: "96px" }, oninput: (e) => { d.stock[c.code] = e.target.value; } })),
      h("td", {}, h("span", { class: "muted small" }, "g")),
      h("td", {}, h("button", { class: "ghost small danger", onclick: () => { removeComponent(idx); }, title: "删除成分" }, "删除")),
    ),
  );
  return h("div", { class: "stack" },
    h("div", { class: "table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "代码"), h("th", {}, "名称"), h("th", {}, "下限"), h("th", {}, "上限"), h("th", {}, "账面库存"), h("th", {}, "单位"), h("th", {}, ""))),
        h("tbody", {}, ...rows),
      ),
    ),
    h("div", { class: "inline" },
      h("button", { class: "small", onclick: () => {
        const code = String.fromCharCode(65 + d.components.length);
        d.components.push({ code, name: `成分 ${code}`, lower: 0, upper: Number(d.total), unit: "g" });
        d.stock[code] = 0;
        rerender();
      } }, "+ 添加成分"),
      h("span", { class: "small muted" }, "这里填写账面目标库存；保存时差额以「库存调整」记入台账，不覆盖历史入库与投料记录。"),
    ),
  );
}

function removeComponent(idx) {
  const d = ensureDraft();
  const code = d.components[idx].code;
  d.components.splice(idx, 1);
  d.forbidden_pairs = d.forbidden_pairs.filter((p) => !p.includes(code));
  d.forbidden_groups = d.forbidden_groups.filter((g) => !g.includes(code));
  rerender();
}

function relationEditor() {
  const d = ensureDraft();
  const codes = d.components.map((c) => c.code);
  const pairA = h("select", {}, ...codes.map((c) => h("option", {}, c)));
  const pairB = h("select", {}, ...codes.map((c) => h("option", {}, c)));
  const groupSel = h("select", { multiple: true, size: Math.min(Math.max(codes.length, 2), 5), style: { "min-width": "120px" } }, ...codes.map((c) => h("option", { value: c }, c)));

  return h("div", { class: "stack" },
    h("div", { class: "field" },
      h("label", {}, "禁配对（两种原料不能同时出现）"),
      h("div", { class: "chips" },
        ...d.forbidden_pairs.map((p, i) =>
          h("span", { class: "chip" }, `${p[0]} ✕ ${p[1]}`, h("button", { onclick: () => { d.forbidden_pairs.splice(i, 1); rerender(); }, "aria-label": "删除" }, "✕"))),
      ),
      h("div", { class: "inline", style: { "margin-top": "6px" } },
        pairA, h("span", { class: "muted" }, "✕"), pairB,
        h("button", { class: "small", onclick: () => {
          const a = pairA.value, b = pairB.value;
          if (a === b) { toast("不能把同一成分与自身禁配", "bad"); return; }
          const key = [a, b].sort().join("|");
          if (d.forbidden_pairs.some((p) => [...p].sort().join("|") === key)) { toast("该禁配对已存在", ""); return; }
          d.forbidden_pairs.push([a, b]);
          rerender();
        } }, "添加禁配"),
      ),
    ),
    h("div", { class: "field" },
      h("label", {}, "互斥组（组内最多只能有一种原料 > 0）"),
      h("div", { class: "chips" },
        ...d.forbidden_groups.map((g, i) =>
          h("span", { class: "chip" }, g.join(" / "), h("button", { onclick: () => { d.forbidden_groups.splice(i, 1); rerender(); }, "aria-label": "删除" }, "✕"))),
      ),
      h("div", { class: "inline", style: { "margin-top": "6px" } },
        groupSel,
        h("button", { class: "small", onclick: () => {
          const chosen = [...groupSel.selectedOptions].map((o) => o.value);
          if (chosen.length < 2) { toast("互斥组至少选择两种成分", "bad"); return; }
          d.forbidden_groups.push(chosen);
          rerender();
        } }, "添加互斥组"),
      ),
    ),
  );
}

function draftCheck() {
  const d = ensureDraft();
  const codes = d.components.map((c) => c.code);
  const inputs = Object.fromEntries(codes.map((c) => [c, h("input", { type: "number", step: "any", value: 0, style: { width: "78px" } })]));
  const out = h("div", {});
  const run = async () => {
    const amounts = Object.fromEntries(codes.map((c) => [c, Number(inputs[c].value || 0)]));
    try {
      const res = await api.post(`/api/studies/${store.studyId}/region/check`, { amounts });
      mount(out,
        res.feasible
          ? h("div", { class: "note-item good" }, h("div", { class: "title" }, "可行"), h("p", { class: "small muted" }, `总和 ${num(res.sum, 2)}`))
          : h("div", { class: "note-item bad" },
              h("div", { class: "title" }, "不可行"),
              h("ul", {}, ...res.violations.map((v) => h("li", {}, `${v.message}${v.kind === "sum" ? `（实际 ${num(v.sum, 2)}）` : ""}`)))),
      );
    } catch (err) {
      errorToast(err);
    }
  };
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "配方可行性试算"), h("span", { class: "hint" }, "只判断，不写入试验区")),
    h("div", { class: "inline" }, ...codes.map((c) => h("label", { class: "small secondary" }, `${c} `, inputs[c]))),
    h("div", { class: "inline", style: { "margin-top": "10px" } }, h("button", { class: "small primary", onclick: run }, "检查"), out),
  );
}

function preview() {
  const d = ensureDraft();
  const region = store.region;
  const codes = d.components.map((c) => c.code);
  const total = region ? region.total : Number(d.total);
  if (!region) return h("div", { class: "empty" }, "尚无区域预览");
  const norm = (pairs) => JSON.stringify(pairs.map((p) => [...p].sort()).sort((a, b) => a.join("|").localeCompare(b.join("|"))));
  const dirty =
    Number(d.total) !== region.total ||
    codes.join("|") !== region.codes.join("|") ||
    d.components.some((c, i) => Number(c.lower) !== region.lower[i] || Number(c.upper) !== region.upper[i]) ||
    norm(d.forbidden_pairs) !== norm(region.forbidden_pairs) ||
    norm(d.forbidden_groups) !== norm(region.forbidden_groups) ||
    Number(d.n_steps) !== region.n_steps;
  return h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("h2", {}, "可行区域预览"),
      h("div", { class: "inline" },
        dirty ? h("span", { class: "badge warn" }, "显示已保存的约束；保存后更新") : null,
        h("span", { class: "badge info" }, `候选 ${region.stats.candidate_count}`),
        h("span", { class: "badge" }, `格点 ${region.stats.lattice_count}`),
        h("span", { class: "badge" }, `顶点 ${region.stats.vertex_count}`),
        h("span", { class: "badge warn" }, `禁配剔除 ${region.stats.removed_by_forbidden}`))),
    region.blocking.length
      ? h("div", { class: "note-item bad" }, h("div", { class: "title" }, "区域为空或被约束排除"), h("ul", {}, ...region.blocking.map((b) => h("li", {}, b.message))))
      : null,
    ternaryPlot({
      codes: region.codes,
      total: region.total,
      regionPoints: region.points,
      layers: [],
      lower: Object.fromEntries(region.codes.map((c, i) => [c, region.lower[i]])),
      upper: Object.fromEntries(region.codes.map((c, i) => [c, region.upper[i]])),
    }),
    h("p", { class: "small muted" }, `三元投影以 ${region.codes[0]}、${region.codes[1]} 与其余 ${region.codes.slice(2).join("+")} 为三个顶点（三者恒定求和为总量）。总量 ${region.total}，网格 ${region.n_steps} 份；候选点由精确整数配比生成，不做事后归一化。`),
  );
}

let rootEl = null;
function rerender() {
  if (rootEl) render(rootEl);
}

export function regionView(root) {
  rootEl = root;
  render(root);
}

function render(root) {
  const d = ensureDraft();
  mount(root,
    h("div", { class: "stack" },
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "试验区设置"), h("span", { class: "hint" }, "总量、上下限、禁配与加工档位")),
        h("div", { class: "form-row" },
          field("配方总量", h("input", { type: "number", step: "any", value: d.total, oninput: (e) => { d.total = e.target.value; } })),
          field("网格份数（解析精度）", h("input", { type: "number", step: "1", min: "1", max: "200", value: d.n_steps, oninput: (e) => { d.n_steps = e.target.value; } })),
          field("单批投料质量", h("input", { type: "number", step: "any", value: d.charge_mass, oninput: (e) => { d.charge_mass = e.target.value; } })),
          field("试验批量", h("input", { type: "number", step: "1", min: "1", value: d.batch_size, oninput: (e) => { d.batch_size = e.target.value; } })),
          field("响应名称", h("input", { value: d.response_name, oninput: (e) => { d.response_name = e.target.value; } })),
          field("模型", h("select", { onchange: (e) => { d.model = e.target.value; } },
            h("option", { value: "linear", selected: d.model === "linear" }, "线性（主效应）"),
            h("option", { value: "quadratic", selected: d.model === "quadratic" }, "二次（含可共存交互）"))),
          field("加工档位（逗号分隔）", h("input", { value: d.process_levels.join(","), oninput: (e) => { d.process_levels = e.target.value.split(",").map((x) => x.trim()).filter(Boolean); } })),
          field("每档最少次数", h("input", { type: "number", step: "1", min: "1", value: d.min_per_level, oninput: (e) => { d.min_per_level = e.target.value; } })),
        ),
        h("div", { class: "inline", style: { "margin-top": "10px" } },
          labelCheck("纳入加工档位", d.include_process, (v) => { d.include_process = v; }),
          labelCheck("允许重复点", d.allow_replicates, (v) => { d.allow_replicates = v; }),
        ),
        h("hr", { class: "hr" }),
        componentEditor(),
        h("hr", { class: "hr" }),
        relationEditor(),
        h("div", { class: "inline", style: { "margin-top": "16px" } },
          h("button", { class: "primary", onclick: save }, "保存试验区"),
          h("button", { onclick: () => { resetDraft(); render(root); } }, "放弃修改"),
          h("span", { class: "small muted" }, "保存后区域与候选点会重新计算。"),
        ),
      ),
      preview(),
      draftCheck(),
    ),
  );
}

function field(label, control) {
  return h("div", { class: "field" }, h("label", {}, label), control);
}

function labelCheck(label, checked, onChange) {
  const input = h("input", { type: "checkbox", checked: !!checked, onchange: (e) => { onChange(e.target.checked); render(rootEl); } });
  return h("label", { class: "inline small" }, input, label);
}
