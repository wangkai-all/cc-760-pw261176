import { api, h, mount, toast, errorToast, num } from "../ui.js";
import { store, refreshAll } from "../app.js";

const SAMPLE = `[
  {"label":"H-201","amounts":{"A":50,"B":30,"C":20,"D":0,"E":0},"level":"低","response":41.2},
  {"label":"H-202","amounts":{"A":40,"B":40,"C":20,"D":0,"E":0},"level":"中","response":43.8}
]`;

function importDialog() {
  const textarea = h("textarea", {
    rows: 10,
    style: { width: "100%", "font-family": "ui-monospace, monospace", "font-size": "12px" },
  }, SAMPLE);
  const out = h("div", {});
  const backdrop = h("div", { class: "modal-backdrop", onclick: (e) => { if (e.target === backdrop) backdrop.remove(); } },
    h("div", { class: "modal" },
      h("h2", {}, "导入历史配方"),
      h("p", { class: "small muted" }, "JSON 数组：label、amounts（每个成分）、level（可选）、response（缺失即记为失败）。违反区域约束的配方会入库但不参与拟合。"),
      h("div", { class: "body" }, textarea),
      out,
      h("div", { class: "actions" },
        h("button", { onclick: () => backdrop.remove() }, "取消"),
        h("button", { class: "primary", onclick: async () => {
          let entries;
          try {
            entries = JSON.parse(textarea.value);
          } catch (err) {
            toast("JSON 解析失败：" + err.message, "bad");
            return;
          }
          try {
            const res = await api.post(`/api/studies/${store.studyId}/history/import`, { entries });
            out.replaceChildren(h("div", { class: "note-item good" },
              h("div", { class: "title" }, `导入 ${res.created} 条`),
              res.rejected.length ? h("ul", {}, ...res.rejected.map((r) => h("li", {}, `#${r.index}：${r.reason}`))) : null));
            await refreshAll();
          } catch (err) {
            errorToast(err);
          }
        } }, "导入"),
      ),
    ),
  );
  document.body.appendChild(backdrop);
}

export function historyView(root) {
  const d = store.diagnostics;
  const cfg = store.study.config;
  const codes = cfg.components.map((c) => c.code);
  const entries = (store.history && store.history.entries) || [];
  const excluded = new Set((d?.excluded || []).map((e) => e.ref));
  const rows = entries.map((e) => h("tr", { style: e.response === null ? { opacity: 0.7 } : {} },
    h("td", {}, e.label),
    ...codes.map((c) => h("td", { class: "num" }, num(e.amounts[c], 1))),
    h("td", {}, e.level || "—"),
    h("td", { class: "num" }, e.response === null ? h("span", { class: "badge" }, "失败") : num(e.response, 2)),
    h("td", { class: "small muted" }, e.note || "—"),
    h("td", {}, excluded.has(e.label) ? h("span", { class: "badge warn" }, "区域外·未拟合") : e.response === null ? h("span", { class: "badge" }, "不参与拟合") : h("span", { class: "badge good" }, "已拟合")),
  ));

  mount(root,
    h("div", { class: "stack" },
      h("div", { class: "card" },
        h("div", { class: "card-head" },
          h("h2", {}, "历史配方"),
          h("div", { class: "inline" },
            h("button", { onclick: importDialog }, "导入 JSON"),
          )),
        entries.length
          ? h("div", { class: "table-wrap" }, h("table", {},
              h("thead", {}, h("tr", {}, h("th", {}, "标签"), ...codes.map((c) => h("th", { class: "num" }, c)), h("th", {}, "档位"), h("th", { class: "num" }, "响应"), h("th", {}, "备注"), h("th", {}, "拟合状态"))),
              h("tbody", {}, ...rows),
            ))
          : h("div", { class: "empty" }, "尚无历史数据，可导入 JSON 或载入演示。"),
      ),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "已投料试验的响应")),
        (d?.history || []).filter((r) => r.source === "run").length
          ? h("div", { class: "table-wrap" }, h("table", {},
              h("thead", {}, h("tr", {}, h("th", {}, "试验"), ...codes.map((c) => h("th", { class: "num" }, c)), h("th", {}, "档位"), h("th", { class: "num" }, "响应"))),
              h("tbody", {}, ...d.history.filter((r) => r.source === "run").map((r) =>
                h("tr", {},
                  h("td", {}, r.ref),
                  ...codes.map((c) => h("td", { class: "num" }, num(r.amounts[c], 1))),
                  h("td", {}, r.level || "—"),
                  h("td", { class: "num" }, num(r.response, 2)),
                ))),
            ))
          : h("div", { class: "empty" }, "还没有录入过试验响应。"),
      ),
    ),
  );
}
