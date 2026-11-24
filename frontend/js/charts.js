/* SVG charts. Palette comes from CSS custom properties in styles.css
   (validated categorical slots, single-hue sequential ramp, status tokens). */

import { h, num, pct } from "./ui.js";

const NS = "http://www.w3.org/2000/svg";

function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    el.setAttribute(k, v);
  }
  for (const k of kids.flat(4)) {
    if (k === null || k === undefined) continue;
    el.appendChild(typeof k === "string" ? document.createTextNode(k) : k);
  }
  return el;
}

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function tooltipHost() {
  const tip = h("div", { class: "chart-tooltip tooltip" });
  return tip;
}

function pretty(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (typeof v === "number") return num(v, 2);
  return String(v);
}

/* ------------------------------------------------------------------ ternary */

/**
 * Three-part projection of a mixture.
 *
 * Corners are ``codes[0]``, ``codes[1]`` and "everything else".  Aggregating
 * the remaining components is exact for a fixed-total mixture: the three parts
 * always sum to the batch total, so no renormalisation is involved.  Plotting
 * only A/B/C and dropping D/E would collapse every D/E-bearing formulation
 * onto one corner and hide exactly the variation the design is buying.
 */
export function ternaryParts(amounts, total, codes) {
  const c0 = codes[0];
  const c1 = codes[1];
  const restCodes = codes.slice(2);
  const a = (amounts[c0] || 0) / total;
  const b = (amounts[c1] || 0) / total;
  const rest = restCodes.reduce((s, c) => s + (amounts[c] || 0), 0) / total;
  return { a, b, rest, c0, c1, restCodes };
}

export function ternaryPoint(amounts, total, codes) {
  const { a, b, rest } = ternaryParts(amounts, total, codes);
  void a;
  return { x: b + 0.5 * rest, y: (Math.sqrt(3) / 2) * rest };
}

export function ternaryPlot({
  codes,
  total,
  regionPoints = [],
  layers = [],
  height = 380,
  title = "配方组成（三元投影）",
  lower = {},
  upper = {},
}) {
  const W = 560;
  const H = height;
  const pad = { l: 44, r: 26, t: 34, b: 66 };
  const iw = W - pad.l - pad.r;
  const ih = H - pad.t - pad.b;
  const h3 = Math.sqrt(3) / 2;
  const size = Math.max(60, Math.min(iw, ih / h3));
  const ox = pad.l + (iw - size) / 2;
  const oy = pad.t;
  const restCodes = codes.slice(2);
  const restLabel = restCodes.length > 1 ? `其余 ${restCodes.join("+")}` : restCodes[0] || "其余";
  const px = (a, b, rest) => ({ x: ox + (b + 0.5 * rest) * size, y: oy + (1 - rest) * h3 * size });
  const toPx = (amounts) => {
    const p = ternaryParts(amounts, total, codes);
    return px(p.a, p.b, p.rest);
  };
  const tri = [px(1, 0, 0), px(0, 1, 0), px(0, 0, 1)];
  const tip = tooltipHost();
  const wrap = h("div", { class: "chart" }, tip);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
  wrap.appendChild(svg);

  const line = (p0, p1) => [p0, p1];
  const path = (pts) => pts.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ") + " Z";
  svg.appendChild(s("path", { d: path(tri), fill: "var(--surface-2)", stroke: "var(--axis)", "stroke-width": 1 }));

  // Gridlines for each of the three parts at 20% steps.
  for (let k = 1; k < 5; k++) {
    const v = k / 5;
    const rest = 1 - v;
    const segments = [
      // a = v : from (b=0, rest=1-v) to (b=1-v, rest=0)
      line(px(v, 0, rest), px(v, rest, 0)),
      // b = v
      line(px(0, v, rest), px(rest, v, 0)),
      // rest = v
      line(px(0, rest, v), px(rest, 0, v)),
    ];
    for (const [p0, p1] of segments) {
      svg.appendChild(s("line", { x1: p0.x, y1: p0.y, x2: p1.x, y2: p1.y, stroke: "var(--grid)", "stroke-width": 1 }));
    }
  }

  // Region lattice: small hollow dots, the exploration support.
  const regionLayer = s("g", { class: "region-layer" });
  for (const p of regionPoints) {
    const pt = toPx(p.amounts);
    regionLayer.appendChild(
      s("circle", { cx: pt.x, cy: pt.y, r: 2.4, fill: "var(--surface-1)", stroke: "var(--text-muted)", "stroke-width": 1, opacity: 0.7 }),
    );
  }
  svg.appendChild(regionLayer);

  // Bound lines for the two explicit axes: constant coordinate = straight edge
  // parallel to the opposite side.
  const boundLayer = s("g", { class: "bound-layer" });
  for (const [code, idx] of [[codes[0], "a"], [codes[1], "b"]]) {
    for (const [kind, value] of [["lo", lower[code]], ["hi", upper[code]]]) {
      if (value === undefined || value === null) continue;
      const frac = value / total;
      if (frac <= 0.001 || frac >= 0.999) continue;
      const rest = 1 - frac;
      const [p0, p1] = idx === "a" ? [px(frac, 0, rest), px(frac, rest, 0)] : [px(0, frac, rest), px(rest, frac, 0)];
      boundLayer.appendChild(
        s("line", {
          x1: p0.x, y1: p0.y, x2: p1.x, y2: p1.y,
          stroke: kind === "lo" ? "var(--series-4)" : "var(--series-7)",
          "stroke-width": 1.4,
          "stroke-dasharray": kind === "lo" ? "5 4" : "2 3",
          opacity: 0.9,
        }),
      );
    }
  }
  svg.appendChild(boundLayer);

  // Data layers: each {name, color, points:[{amounts, label, meta}]}
  for (const layer of layers) {
    const g = s("g", { class: "layer" });
    layer.points.forEach((p) => {
      const pt = toPx(p.amounts);
      const r = p.radius || 4.6;
      const dot = s("circle", {
        cx: pt.x,
        cy: pt.y,
        r,
        fill: p.hollow ? "var(--surface-1)" : layer.color,
        stroke: p.hollow ? layer.color : "var(--surface-1)",
        "stroke-width": p.hollow ? 1.6 : 2,
        opacity: p.faded ? 0.55 : 1,
      });
      const hit = s("circle", { cx: pt.x, cy: pt.y, r: Math.max(11, r + 6), fill: "transparent", style: "cursor:pointer" });
      const show = (evt) => {
        tip.innerHTML = "";
        tip.append(
          h("div", { class: "t" }, p.label || layer.name),
          h("div", { class: "r" }, h("span", {}, "组成"), h("b", {}, codes.map((c) => `${c} ${num(p.amounts[c], 1)}`).join(" · "))),
          ...(p.meta || []).map(([k, v]) => h("div", { class: "r" }, h("span", {}, k), h("b", {}, pretty(v)))),
          h("div", { class: "r" }, h("span", {}, "来源"), h("b", {}, layer.name)),
        );
        const box = wrap.getBoundingClientRect();
        tip.classList.add("on");
        tip.style.left = `${Math.min(evt.clientX - box.left + 14, box.width - 210)}px`;
        tip.style.top = `${Math.max(evt.clientY - box.top - 12, 0)}px`;
        dot.setAttribute("r", r + 2.5);
      };
      const hide = () => {
        tip.classList.remove("on");
        dot.setAttribute("r", r);
      };
      hit.addEventListener("mousemove", show);
      hit.addEventListener("mouseleave", hide);
      hit.addEventListener("focus", show);
      hit.addEventListener("blur", hide);
      hit.setAttribute("tabindex", 0);
      hit.setAttribute("role", "button");
      hit.setAttribute("aria-label", `${p.label || layer.name}：${codes.map((c) => `${c} ${num(p.amounts[c], 1)}`).join("，")}`);
      g.appendChild(hit);
      g.appendChild(dot);
    });
    svg.appendChild(g);
  }

  // Corner labels.
  const corners = [
    { p: tri[0], text: codes[0], anchor: "end", dy: -6, dx: -4 },
    { p: tri[1], text: codes[1], anchor: "start", dy: 18, dx: 4 },
    { p: tri[2], text: restLabel, anchor: "middle", dy: -10, dx: 0 },
  ];
  for (const c of corners) {
    svg.appendChild(
      s("text", { x: c.p.x + c.dx, y: c.p.y + c.dy, "text-anchor": c.anchor, fill: "var(--text-secondary)", "font-size": 12, "font-weight": 600 }, c.text),
    );
  }
  // Bottom axis = share of codes[1]; left axis = share of the rest group.
  for (let k = 0; k <= 5; k++) {
    const t = k / 5;
    const bottom = px(1 - t, t, 0);
    svg.appendChild(s("text", { x: bottom.x, y: bottom.y + 15, "text-anchor": "middle", fill: "var(--text-muted)", "font-size": 10 }, `${Math.round(t * 100)}`));
    if (k > 0 && k < 5) {
      const left = px(1 - t, 0, t);
      svg.appendChild(s("text", { x: left.x - 5, y: left.y + 3, "text-anchor": "end", fill: "var(--text-muted)", "font-size": 10 }, `${Math.round(t * 100)}`));
    }
  }
  svg.appendChild(
    s("text", { x: W / 2, y: H - 26, "text-anchor": "middle", fill: "var(--text-muted)", "font-size": 10 },
      `底边 = ${codes[1]} 质量分数（%），左边 = ${restCodes.join("+")} 合计（%）；虚线=下限，点线=上限`),
  );
  svg.appendChild(
    s("text", { x: W / 2, y: H - 12, "text-anchor": "middle", fill: "var(--text-muted)", "font-size": 10 },
      `总量 ${num(total, 0)}，三点始终求和为该总量`),
  );

  const legend = h(
    "div",
    { class: "legend" },
    ...layers
      .filter((l) => l.points.length)
      .map((l) =>
        h("span", {},
          h("i", { style: { background: l.hollow ? "transparent" : l.color, border: l.hollow ? `2px solid ${l.color}` : "none" } }),
          `${l.name}（${l.points.length}）`),
      ),
    regionPoints.length
      ? h("span", {}, h("i", { style: { background: "var(--surface-1)", border: "1px solid var(--text-muted)" } }), `可行格点（${regionPoints.length}）`)
      : null,
  );
  const table = regionTable(codes, layers);
  return h("div", {}, wrap, legend, table);
}

function regionTable(codes, layers) {
  const rows = [];
  for (const layer of layers) {
    for (const p of layer.points) {
      rows.push(
        h("tr", {},
          h("td", {}, layer.name),
          h("td", {}, p.label || "—"),
          ...codes.map((c) => h("td", { class: "num" }, num(p.amounts[c], 1))),
          h("td", { class: "num" }, num(codes.reduce((acc, c) => acc + (p.amounts[c] || 0), 0), 1)),
        ),
      );
    }
  }
  return h(
    "details",
    { class: "table-view" },
    h("summary", {}, "表格视图"),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, h("th", {}, "来源"), h("th", {}, "标签"), ...codes.map((c) => h("th", { class: "num" }, c)), h("th", { class: "num" }, "总和"))),
        h("tbody", {}, ...rows),
      ),
    ),
  );
}

/* --------------------------------------------------------------- bar charts */

/**
 * Horizontal bar chart with one series. Values are (criterion contributions).
 * Highlights the top bar; others use the same hue at lower opacity so rank is
 * not encoded as hue.
 */
export function barChart({ items, valueLabel = "信息增益", height, formatter = (v) => num(v, 2), color = "var(--series-1)" }) {
  const W = 520;
  const rowH = 22;
  const H = height || Math.max(90, items.length * rowH + 34);
  const pad = { l: 92, r: 54, t: 8, b: 22 };
  const iw = W - pad.l - pad.r;
  const max = Math.max(...items.map((d) => Math.abs(d.value)), 1e-9);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": valueLabel });
  const tip = tooltipHost();
  const wrap = h("div", { class: "chart" }, tip);
  wrap.appendChild(svg);
  const best = Math.max(...items.map((d) => d.value));
  items.forEach((d, i) => {
    const y = pad.t + i * rowH;
    const w = Math.max(2, (Math.abs(d.value) / max) * iw);
    svg.appendChild(
      s("text", { x: pad.l - 8, y: y + rowH / 2 + 4, "text-anchor": "end", fill: "var(--text-secondary)", "font-size": 11 }, d.label),
    );
    const bar = s("rect", {
      x: pad.l,
      y: y + 4,
      width: w,
      height: rowH - 10,
      rx: 3,
      fill: color,
      opacity: d.value === best ? 1 : 0.42,
    });
    bar.addEventListener("mousemove", (evt) => {
      tip.innerHTML = "";
      tip.append(h("div", { class: "t" }, d.label), h("div", { class: "r" }, h("span", {}, valueLabel), h("b", {}, formatter(d.value))));
      const box = wrap.getBoundingClientRect();
      tip.classList.add("on");
      tip.style.left = `${Math.min(evt.clientX - box.left + 12, box.width - 180)}px`;
      tip.style.top = `${Math.max(evt.clientY - box.top - 12, 0)}px`;
    });
    bar.addEventListener("mouseleave", () => tip.classList.remove("on"));
    svg.appendChild(bar);
    svg.appendChild(
      s("text", { x: pad.l + w + 6, y: y + rowH / 2 + 4, fill: "var(--text-secondary)", "font-size": 11 }, formatter(d.value)),
    );
  });
  svg.appendChild(s("line", { x1: pad.l, y1: pad.t, x2: pad.l, y2: H - pad.b, stroke: "var(--axis)", "stroke-width": 1 }));
  return h("div", {}, wrap, h(
    "details",
    { class: "table-view" },
    h("summary", {}, "表格视图"),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "试验"), h("th", { class: "num" }, valueLabel))),
      h("tbody", {}, ...items.map((d) => h("tr", {}, h("td", {}, d.label), h("td", { class: "num" }, formatter(d.value))))),
    )),
  ));
}

/* --------------------------------------------------------------- components */

/**
 * Stacked composition bars, one row per run. Parts are separated by a 2px
 * surface gap; a legend names the components; labels only where they fit.
 */
export function compositionBars({ runs, codes, names, total, limit = 24, colors = [] }) {
  const rows = runs.slice(0, limit);
  const W = 560;
  const rowH = 24;
  const H = rows.length * rowH + 40;
  const pad = { l: 42, r: 12, t: 6, b: 26 };
  const iw = W - pad.l - pad.r;
  const palette = colors.length ? colors : codes.map((_, i) => `var(--series-${(i % 8) + 1})`);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "各试验配方组成" });
  const tip = tooltipHost();
  const wrap = h("div", { class: "chart" }, tip);
  wrap.appendChild(svg);
  rows.forEach((run, r) => {
    const y = pad.t + r * rowH;
    const label = `#${run.ordinal}`;
    svg.appendChild(s("text", { x: pad.l - 8, y: y + rowH / 2 + 4, "text-anchor": "end", fill: "var(--text-muted)", "font-size": 10 }, label));
    let x = pad.l;
    codes.forEach((code, i) => {
      const frac = (run.amounts[code] || 0) / total;
      if (frac <= 0) return;
      const w = frac * iw;
      const gap = 2;
      const rect = s("rect", {
        x: x + (i ? gap / 2 : 0),
        y: y + 6,
        width: Math.max(w - gap, 0.5),
        height: rowH - 12,
        fill: palette[i % palette.length],
        opacity: run.status === "cancelled" ? 0.35 : 1,
      });
      rect.addEventListener("mousemove", (evt) => {
        tip.innerHTML = "";
        tip.append(
          h("div", { class: "t" }, `试验 ${label} · ${run.level || "无档位"}`),
          h("div", { class: "r" }, h("span", {}, `${code} ${names[code] || ""}`), h("b", {}, `${num(run.amounts[code], 2)} (${pct(frac)})`)),
          h("div", { class: "r" }, h("span", {}, "状态"), h("b", {}, run.status)),
        );
        const box = wrap.getBoundingClientRect();
        tip.classList.add("on");
        tip.style.left = `${Math.min(evt.clientX - box.left + 12, box.width - 190)}px`;
        tip.style.top = `${Math.max(evt.clientY - box.top - 12, 0)}px`;
      });
      rect.addEventListener("mouseleave", () => tip.classList.remove("on"));
      svg.appendChild(rect);
      x += w;
    });
    if (run.level) {
      svg.appendChild(s("text", { x: W - pad.r, y: y + rowH / 2 + 4, "text-anchor": "end", fill: "var(--text-muted)", "font-size": 10 }, run.level));
    }
  });
  const legend = h("div", { class: "legend" },
    ...codes.map((c, i) => h("span", {}, h("i", { style: { background: palette[i % palette.length], "border-radius": "2px" } }), `${c}${names[c] ? " " + names[c] : ""}`)),
  );
  return h("div", {}, wrap, legend);
}

/* --------------------------------------------------------------- term chart */

/**
 * Two-series chart of posterior SD per model term, before vs after the batch.
 * Bars are paired on one shared axis; both series are directly labelled by the
 * legend and the numbers sit in the table view.
 */
export function termSdChart({ terms }) {
  const W = 560;
  const rowH = 26;
  const H = terms.length * rowH + 22;
  const pad = { l: 138, r: 56, t: 6, b: 16 };
  const iw = W - pad.l - pad.r;
  const max = Math.max(...terms.flatMap((t) => [t.before_sd, t.after_sd]), 1e-9);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "各参数后验标准差与先验之比：追加前与追加后" });
  const tip = tooltipHost();
  const wrap = h("div", { class: "chart" }, tip);
  wrap.appendChild(svg);
  terms.forEach((t, i) => {
    const y = pad.t + i * rowH;
    svg.appendChild(s("text", { x: pad.l - 8, y: y + rowH / 2 + 4, "text-anchor": "end", fill: t.identifiable_before ? "var(--text-secondary)" : "var(--text-muted)", "font-size": 11 }, t.term));
    const wb = Math.max(1, (t.before_sd / max) * iw);
    const wa = Math.max(1, (t.after_sd / max) * iw);
    const before = s("rect", { x: pad.l, y: y + 4, width: wb, height: 7, rx: 3, fill: "var(--series-2)", opacity: 0.85 });
    const after = s("rect", { x: pad.l, y: y + 13, width: wa, height: 7, rx: 3, fill: "var(--series-3)" });
    for (const [el, label, val] of [[before, "追加前", t.before_sd], [after, "追加后", t.after_sd]]) {
      el.addEventListener("mousemove", (evt) => {
        tip.innerHTML = "";
        tip.append(h("div", { class: "t" }, t.term), h("div", { class: "r" }, h("span", {}, label), h("b", {}, num(val, 3))));
        const box = wrap.getBoundingClientRect();
        tip.classList.add("on");
        tip.style.left = `${Math.min(evt.clientX - box.left + 12, box.width - 190)}px`;
        tip.style.top = `${Math.max(evt.clientY - box.top - 12, 0)}px`;
      });
      el.addEventListener("mouseleave", () => tip.classList.remove("on"));
    }
    svg.appendChild(before);
    svg.appendChild(after);
    svg.appendChild(s("text", { x: pad.l + Math.max(wb, wa) + 6, y: y + rowH / 2 + 4, fill: "var(--text-secondary)", "font-size": 10 }, `${num(t.before_sd, 2)} → ${num(t.after_sd, 2)}`));
  });
  svg.appendChild(s("line", { x1: pad.l, y1: pad.t, x2: pad.l, y2: H - pad.b, stroke: "var(--axis)", "stroke-width": 1 }));
  const legend = h("div", { class: "legend" },
    h("span", {}, h("i", { style: { background: "var(--series-2)", "border-radius": "2px" } }), "追加前（仅历史）"),
    h("span", {}, h("i", { style: { background: "var(--series-3)", "border-radius": "2px" } }), "追加后（含本批）"),
  );
  const table = h(
    "details",
    { class: "table-view" },
    h("summary", {}, "表格视图"),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "参数"), h("th", { class: "num" }, "追加前 SD/先验"), h("th", { class: "num" }, "追加后 SD/先验"), h("th", {}, "追加前可辨识"), h("th", {}, "追加后可辨识"))),
      h("tbody", {}, ...terms.map((t) => h("tr", {},
        h("td", {}, t.term),
        h("td", { class: "num" }, num(t.before_sd, 3)),
        h("td", { class: "num" }, num(t.after_sd, 3)),
        h("td", {}, t.identifiable_before ? "是" : "否"),
        h("td", {}, t.identifiable_after ? "是" : "否"),
      ))),
    )),
  );
  return h("div", {}, wrap, legend, table);
}

/* --------------------------------------------------------------- scatter */

/** Simple scatter: x/y with tooltips. One series per layer. */
export function scatter({ layers, xLabel, yLabel, height = 300, xFormat = (v) => num(v, 1), yFormat = (v) => num(v, 1) }) {
  const W = 560;
  const H = height;
  const pad = { l: 56, r: 18, t: 14, b: 44 };
  const iw = W - pad.l - pad.r;
  const ih = H - pad.t - pad.b;
  const xs = layers.flatMap((l) => l.points.map((p) => p.x));
  const ys = layers.flatMap((l) => l.points.map((p) => p.y));
  const xmin = Math.min(...xs, 0);
  const xmax = Math.max(...xs, 1);
  const ymin = Math.min(...ys, 0);
  const ymax = Math.max(...ys, 1);
  const xpad = (xmax - xmin) * 0.06 || 1;
  const ypad = (ymax - ymin) * 0.08 || 1;
  const X = (v) => pad.l + ((v - (xmin - xpad)) / (xmax - xmin + 2 * xpad)) * iw;
  const Y = (v) => pad.t + ih - ((v - (ymin - ypad)) / (ymax - ymin + 2 * ypad)) * ih;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${yLabel} 对 ${xLabel}` });
  const tip = tooltipHost();
  const wrap = h("div", { class: "chart" }, tip);
  wrap.appendChild(svg);
  for (let k = 0; k <= 4; k++) {
    const v = ymin - ypad + ((ymax - ymin + 2 * ypad) * k) / 4;
    const y = Y(v);
    svg.appendChild(s("line", { x1: pad.l, y1: y, x2: pad.l + iw, y2: y, class: "gridline" }));
    svg.appendChild(s("text", { x: pad.l - 8, y: y + 4, "text-anchor": "end", fill: "var(--text-muted)", "font-size": 10 }, yFormat(v)));
  }
  for (let k = 0; k <= 4; k++) {
    const v = xmin - xpad + ((xmax - xmin + 2 * xpad) * k) / 4;
    svg.appendChild(s("text", { x: X(v), y: pad.t + ih + 16, "text-anchor": "middle", fill: "var(--text-muted)", "font-size": 10 }, xFormat(v)));
  }
  svg.appendChild(s("line", { x1: pad.l, y1: pad.t + ih, x2: pad.l + iw, y2: pad.t + ih, class: "axisline" }));
  svg.appendChild(s("line", { x1: pad.l, y1: pad.t, x2: pad.l, y2: pad.t + ih, class: "axisline" }));
  svg.appendChild(s("text", { x: pad.l + iw / 2, y: H - 8, "text-anchor": "middle", fill: "var(--text-secondary)", "font-size": 11 }, xLabel));
  svg.appendChild(s("text", { x: 12, y: pad.t + ih / 2, "text-anchor": "middle", fill: "var(--text-secondary)", "font-size": 11, transform: `rotate(-90 12 ${pad.t + ih / 2})` }, yLabel));
  for (const layer of layers) {
    for (const p of layer.points) {
      const cx = X(p.x);
      const cy = Y(p.y);
      const dot = s("circle", {
        cx, cy, r: p.r || 5, fill: layer.color,
        stroke: "var(--surface-1)", "stroke-width": 2,
        opacity: p.faded ? 0.5 : 1,
      });
      const hit = s("circle", { cx, cy, r: 12, fill: "transparent", tabindex: 0, role: "button", "aria-label": p.label });
      const show = (evt) => {
        tip.innerHTML = "";
        tip.append(h("div", { class: "t" }, p.label), ...(p.meta || []).map(([k, v]) => h("div", { class: "r" }, h("span", {}, k), h("b", {}, v))));
        const box = wrap.getBoundingClientRect();
        tip.classList.add("on");
        const cx0 = evt && evt.clientX !== undefined ? evt.clientX - box.left : cx;
        const cy0 = evt && evt.clientY !== undefined ? evt.clientY - box.top : cy;
        tip.style.left = `${Math.min(cx0 + 12, box.width - 190)}px`;
        tip.style.top = `${Math.max(cy0 - 12, 0)}px`;
        dot.setAttribute("r", (p.r || 5) + 2);
      };
      hit.addEventListener("mousemove", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("mouseleave", () => { tip.classList.remove("on"); dot.setAttribute("r", p.r || 5); });
      hit.addEventListener("blur", () => { tip.classList.remove("on"); dot.setAttribute("r", p.r || 5); });
      svg.appendChild(hit);
      svg.appendChild(dot);
    }
  }
  const legend = h("div", { class: "legend" },
    ...layers.filter((l) => l.points.length).map((l) => h("span", {}, h("i", { style: { background: l.color } }), l.name)));
  return h("div", {}, wrap, legend);
}
