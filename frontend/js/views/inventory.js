import { api, h, mount, toast, errorToast, num } from "../ui.js";
import { store, refreshAll, go, cancelRun } from "../app.js";

function kindLabel(kind) {
  return { receipt: "入库", dose: "投料", adjustment: "调整" }[kind] || kind;
}

export function inventoryView(root) {
  const inv = store.inventory;
  const cfg = store.study.config;
  const codes = cfg.components.map((c) => c.code);
  const receiptForm = (() => {
    const codeSel = h("select", {}, ...codes.map((c) => h("option", {}, c)));
    const qty = h("input", { type: "number", step: "any", min: "0", placeholder: "数量" });
    const note = h("input", { type: "text", placeholder: "备注（可选）" });
    const submit = async () => {
      const delta = Number(qty.value);
      if (!(delta > 0)) { toast("入库数量必须为正", "bad"); return; }
      try {
        await api.post(`/api/studies/${store.studyId}/inventory/receipt`, {
          component_code: codeSel.value, delta, note: note.value || "入库",
        });
        toast("已入库", "good");
        qty.value = ""; note.value = "";
        await refreshAll();
      } catch (err) {
        errorToast(err);
      }
    };
    return h("div", { class: "inline" }, codeSel, qty, note, h("button", { class: "primary", onclick: submit }, "登记入库"));
  })();

  const max = Math.max(...inv.inventory.map((r) => r.balance), 1);
  const rows = inv.inventory.map((row) => {
    const pct = (v) => Math.min(100, Math.max(0, (v / max) * 100));
    return h("tr", {},
      h("td", {}, h("strong", {}, row.code), h("div", { class: "small muted" }, row.name)),
      h("td", { class: "num" }, num(row.balance, 2)),
      h("td", { class: "num" }, num(row.held, 2)),
      h("td", { class: "num" }, row.available < 0 ? h("span", { style: { color: "var(--critical)" } }, num(row.available, 2)) : num(row.available, 2)),
      h("td", { style: { width: "34%" } },
        h("div", { class: "bar-track", title: `余额 ${num(row.balance, 1)}，占用 ${num(row.held, 1)}，可用 ${num(row.available, 1)}` },
          h("div", { class: "bar-fill", style: { width: `${pct(row.available)}%` } }),
          h("div", { class: "bar-fill held", style: { width: `${pct(row.held)}%` } })),
      ),
      h("td", {}, h("span", { class: "muted small" }, row.unit)),
    );
  });

  const holds = inv.holds.length
    ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "试验"), h("th", {}, "任务"), h("th", {}, "占用"), h("th", {}, "操作"))),
        h("tbody", {}, ...inv.holds.map((hold) =>
          h("tr", {},
            h("td", {}, `#${hold.ordinal}`),
            h("td", { class: "small" }, hold.task_name),
            h("td", { class: "small mono" }, Object.entries(hold.usage).filter(([, v]) => v > 0).map(([k, v]) => `${k} ${num(v, 1)}`).join("  ")),
            h("td", {}, h("button", { class: "small danger", onclick: () => cancelRun(hold.run_id, `#${hold.ordinal}`) }, "取消并释放")),
          ))),
      ))
    : h("div", { class: "empty" }, "没有待投料试验占用原料");

  mount(root,
    h("div", { class: "stack" },
      h("div", { class: "grid side" },
        h("div", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "库存与占用"), h("span", { class: "hint" }, "可用 = 账面余额 − 待投料试验占用")),
          h("div", { class: "legend-inline" },
            h("span", {}, h("i", { style: { background: "var(--series-1)" } }), "可用"),
            h("span", {}, h("i", { style: { background: "var(--series-4)" } }), "占用"),
          ),
          h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, "原料"), h("th", { class: "num" }, "余额"), h("th", { class: "num" }, "占用"), h("th", { class: "num" }, "可用"), h("th", {}, "占用量"), h("th", {}, ""))),
            h("tbody", {}, ...rows),
          )),
          h("div", { class: "inline", style: { "margin-top": "12px" } }, receiptForm),
        ),
        h("div", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "占用明细"), h("button", { class: "ghost small", onclick: () => go("tasks") }, "去任务 →")),
          holds,
          h("p", { class: "small muted", style: { "margin-top": "10px" } },
            "占用来自由各设计任务生成的「待投料」试验。确认投料后，占用转为台账扣减；取消试验则释放。多个任务共享同一份库存，不会被重复占用。"),
        ),
      ),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "台账流水"), h("span", { class: "hint" }, "只追加，不覆盖")),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "时间"), h("th", {}, "类型"), h("th", {}, "成分"), h("th", { class: "num" }, "变动"), h("th", {}, "关联试验"), h("th", {}, "备注"))),
          h("tbody", {}, ...inv.ledger.map((e) =>
            h("tr", {},
              h("td", { class: "small muted" }, e.created_at.replace("T", " ").slice(0, 16)),
              h("td", {}, kindLabel(e.kind)),
              h("td", {}, e.component_code),
              h("td", { class: "num", style: { color: e.delta < 0 ? "var(--text-secondary)" : "var(--good-text)" } }, `${e.delta > 0 ? "+" : ""}${num(e.delta, 2)}`),
              h("td", { class: "small" }, e.run_id ? `R${e.run_id}` : "—"),
              h("td", { class: "small muted" }, e.note),
            ),
          )),
        )),
      ),
    ),
  );
}
