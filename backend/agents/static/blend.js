"use strict";
// "Из прошлых поездок": pour past trips (flat colour drops) into a bowl, their tastes blend into a concept for the new trip.
// Uses ui.js globals: $, el, api, toast, S. Server side: GET /history/trips, GET /history/trips/{id}/essence,
// POST /history/chats/{id}/blend. Design and rules: docs/trip-blend.md.

const B = {
  open: false, trips: [], essences: new Map(), // chat_id -> {title, ingredients}
  inBowl: [], off: new Set(), on: new Set(), choice: {}, // ingredient key -> source chat_id for conflicts
  applied: null, // {count, titles} after "Собрать"
  pending: new Set(), leaving: new Set(), // trips flying into / out of the bowl right now
};
const PALETTE = 5;
const DRAG_START_PX = 6;
const short = (title) => (title || "Поездка").split(/[,·(]/)[0].trim();
const tripN = (id) => (B.trips.findIndex((t) => t.chat_id === id) % PALETTE) + 1;
const tripColor = (id) => `var(--trip-${tripN(id)})`;
const tripColor2 = (id) => `var(--trip-${tripN(id)}-b)`;
function paint(node, id) { node.style.setProperty("--c", tripColor(id)); node.style.setProperty("--c2", tripColor2(id)); }
const plural = (n, one, few, many) => (n % 10 === 1 && n % 100 !== 11 ? one : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? few : many);
const storeKey = () => `ekv_blend_${S.chat}`;

function saveBlend() {
  try { localStorage.setItem(storeKey(), JSON.stringify({ inBowl: B.inBowl, off: [...B.off], on: [...B.on], choice: B.choice, applied: B.applied })); }
  catch (e) { /* private mode: the blend lasts this visit */ }
}
function loadBlend() {
  B.chat = S.chat;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(storeKey()) || "null"); } catch (e) { saved = null; }
  Object.assign(B, { inBowl: [], off: new Set(), on: new Set(), choice: {}, applied: null });
  if (saved) Object.assign(B, { inBowl: saved.inBowl || [], off: new Set(saved.off), on: new Set(saved.on), choice: saved.choice || {}, applied: saved.applied });
  B.inBowl = B.inBowl.filter((id) => B.trips.some((t) => t.chat_id === id));
}

async function essence(id) {
  if (!B.essences.has(id)) B.essences.set(id, await api(`/api/v1/history/trips/${id}/essence`));
  return B.essences.get(id);
}

// ---------------------------------------------------------------- the blend itself (pure)
// Same key from several trips merges into one ingredient. Same key with a different meaning ("от 4★" vs "от 3★") is a
// conflict: the user picks which trip's version wins (default: the trip that went into the bowl first). Meaning is the
// text the user sees, so a priority weighted 2.0 in one trip and 2.5 in another is one ingredient, not a conflict.
function mixed() {
  const out = new Map();
  for (const id of B.inBowl) {
    const e = B.essences.get(id);
    if (!e) continue;
    for (const ing of e.ingredients) {
      const sig = ing.text;
      let m = out.get(ing.key);
      if (!m) { m = { key: ing.key, group: ing.group, variants: [], default_on: ing.default_on }; out.set(ing.key, m); }
      let v = m.variants.find((x) => x.sig === sig);
      if (!v) { v = { sig, text: ing.text, why: ing.why, phrase: ing.phrase, sources: [] }; m.variants.push(v); }
      v.sources.push(id);
      m.default_on = m.default_on || ing.default_on;
    }
  }
  for (const m of out.values()) {
    m.conflict = m.variants.length > 1;
    const chosen = B.choice[m.key];
    m.pick = m.variants.find((v) => v.sources.includes(chosen)) || m.variants[0];
    m.active = m.default_on ? !B.off.has(m.key) : B.on.has(m.key);
  }
  return [...out.values()];
}

// ---------------------------------------------------------------- render
function bubble(trip, where) {
  const b = el("button", { type: "button", class: `bub ${where}`, "data-id": trip.chat_id, "aria-label": `${trip.title}: ${where === "bowl" ? "убрать из концепта" : "добавить в концепт"}` },
    el("b", { text: short(trip.title) }),
    el("span", { text: trip.date_from ? new Date(trip.date_from + "T00:00:00").toLocaleDateString("ru-RU", { month: "short", year: "2-digit" }) : "" }));
  paint(b, trip.chat_id);
  b.addEventListener("pointerdown", (e) => startDrag(e, b, trip.chat_id, where));
  return b;
}

function token(trip) {
  const t = el("button", { type: "button", class: "tok", "data-id": trip.chat_id, "aria-label": `${trip.title}: вынуть из концепта` },
    el("i", { "aria-hidden": "true" }), short(trip.title), el("span", { "aria-hidden": "true", text: "↑" }));
  paint(t, trip.chat_id);
  t.addEventListener("pointerdown", (e) => startDrag(e, t, trip.chat_id, "bowl"));
  return t;
}

function dots(sources) {
  return el("span", { class: "dots-src", "aria-hidden": "true" }, sources.map((id) => {
    const d = el("i"); d.style.background = `linear-gradient(140deg, ${tripColor(id)}, ${tripColor2(id)})`; return d;
  }));
}

function chip(m) {
  const toggle = () => {
    const set = m.default_on ? B.off : B.on;
    set.has(m.key) ? set.delete(m.key) : set.add(m.key);
    saveBlend(); renderBlend();
  };
  const c = el("div", { class: `ing ${m.group} ${m.active ? "on" : "off"}`, "data-key": m.key },
    el("button", { type: "button", class: "ing-main", "aria-pressed": String(m.active), title: m.pick.why, onclick: toggle },
      el("span", { class: "ing-mark", "aria-hidden": "true", text: m.active ? "✓" : "+" }),
      el("span", { text: m.conflict ? m.pick.text : m.variants[0].text }),
      dots(m.conflict ? m.pick.sources : m.variants[0].sources)));
  if (m.conflict) {
    c.append(el("span", { class: "ing-alt" }, m.variants.filter((v) => v !== m.pick).map((v) =>
      el("button", { type: "button", title: `взять из ${v.sources.map((id) => short(tripTitle(id))).join(", ")}`,
        onclick: () => { B.choice[m.key] = v.sources[0]; saveBlend(); renderBlend(); } }, "или ", v.text, dots(v.sources)))));
  }
  return c;
}
const tripTitle = (id) => (B.trips.find((t) => t.chat_id === id) || {}).title;

let prevRects = new Map();
function renderBlend() {
  const root = $("blend");
  if (!root) return;
  root.classList.toggle("hidden", !B.open);
  $("chat").classList.toggle("blending", B.open);
  $("blend-btn")?.setAttribute("aria-expanded", String(B.open));
  renderConceptPill();
  if (!B.open) return;
  const shelf = $("blend-shelf"), bowl = $("blend-bowl"), list = $("blend-ings");
  // remember where chips were, to glide them to their new places (FLIP)
  prevRects = new Map([...list.querySelectorAll(".ing")].map((n) => [n.dataset.key, n.getBoundingClientRect()]));

  const away = (id) => B.inBowl.includes(id) || B.pending.has(id) || B.leaving.has(id);
  shelf.replaceChildren(...B.trips.map((t) => away(t.chat_id)
    ? el("span", { class: "bub-slot", "data-id": t.chat_id, "aria-hidden": "true" })
    : bubble(t, "shelf")));
  if (!B.trips.length) shelf.append(el("p", { class: "muted", text: "Здесь появятся ваши прошлые поездки." }));

  // the bowl: one round drop that has swallowed every trip in it; under it, a token per trip to pull it back out
  const inside = B.inBowl.map((id) => B.trips.find((t) => t.chat_id === id)).filter(Boolean);
  $("blend-tokens").replaceChildren(...inside.map(token));
  bowl.classList.toggle("empty", !inside.length);
  bowl.style.setProperty("--n", inside.length);
  renderCore(inside.length, inside.map((t) => [tripColor(t.chat_id), tripColor2(t.chat_id)]).flat());
  $("blend-core-text").textContent = inside.length ? (inside.length === 1 ? `как ${short(inside[0].title)}` : "ваш концепт") : "";

  const ms = mixed();
  const groups = [["Возьмём в новую поездку", ms.filter((m) => m.group !== "context")],
                  ["Контекст: по умолчанию не переносим, включите, если нужно", ms.filter((m) => m.group === "context")]];
  list.replaceChildren(...groups.filter(([, items]) => items.length).flatMap(([title, items]) =>
    [el("p", { class: "ing-head", text: title }), el("div", { class: "ing-row" }, items.map(chip))]));
  flip(list);

  const active = ms.filter((m) => m.active);
  const go = $("blend-go");
  go.disabled = !inside.length;
  go.textContent = inside.length ? `Собрать поездку · ${active.length}` : "Собрать поездку";
}

function flip(list) {
  for (const n of list.querySelectorAll(".ing")) {
    const before = prevRects.get(n.dataset.key), after = n.getBoundingClientRect();
    if (!before) { n.classList.add("enter"); continue; }
    const dx = before.left - after.left, dy = before.top - after.top;
    if (!dx && !dy) continue;
    n.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 320, easing: "cubic-bezier(.2,.9,.3,1.2)" });
  }
}

function renderConceptPill() {
  const pill = $("blend-pill");
  if (!pill) return;
  if (B.chat !== S.chat) { loadBlend(); B.open = false; $("blend")?.classList.add("hidden"); $("chat").classList.remove("blending"); } // another chat now
  const a = B.applied;
  pill.classList.toggle("hidden", !a || B.open);
  if (a) $("blend-pill-text").textContent = `Концепт ${a.titles.map(short).join(" + ")}: ${a.count} ${plural(a.count, "вкус", "вкуса", "вкусов")}`;
}

// ---------------------------------------------------------------- liquid drag and drop
// The bowl's shapes are SVG circles in one group under the "goo" filter (index.html, #goo): drops close to each other
// melt together with a liquid neck. Everything that is part of the concept is filled with ONE gradient in shared
// coordinates (#mass, userSpaceOnUse), so the big drop and a drop sinking into it read as a single mass, not two
// circles with their own colours. A drop in flight wears its trip's own colours; at the moment of contact they fade
// into the mass gradient. Pulling a trip out is the reverse: the drop leaves the mass and gets its own colours back.
let drag = null;
const POUR_MS = 760;
const APPROACH_MS = 520;
const TOUCH_AT = .82; // share of the approach after which the goo necks of the two drops have met
const CORE_BASE = 84; // px, the empty concept; each trip adds 20, capped at 164 (the drop is scaled, see renderCore)
const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs = {}) => { const n = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
const gooRect = () => $("blend-goo").getBoundingClientRect();

function tripGradient(id) { // per-trip two-tone gradient for a drop in flight
  const gid = `tg-${tripN(id)}`;
  if (!document.getElementById(gid)) {
    const g = svgEl("linearGradient", { id: gid, x1: "0", y1: "0", x2: "1", y2: "1" });
    g.append(svgEl("stop", { offset: "0.15", style: `stop-color: ${tripColor(id)}` }), svgEl("stop", { offset: "0.95", style: `stop-color: ${tripColor2(id)}` }));
    $("blend-defs").append(g);
  }
  return `url(#${gid})`;
}

function drop(id, rect, { mass = false } = {}) { // a drop in the goo group at a viewport rect; `mass`: born inside the concept
  const g = gooRect(), cx = rect.left + rect.width / 2 - g.left, cy = rect.top + rect.height / 2 - g.top, r = rect.width / 2;
  const d = svgEl("g", { class: "drop", "data-id": id });
  d.append(svgEl("circle", { class: "mass", cx, cy, r, fill: "url(#mass)" }),
           svgEl("circle", { class: "own", cx, cy, r, fill: tripGradient(id), opacity: mass ? 0 : 1 }));
  $("blend-mass").append(d);
  return d;
}
function moveDrop(d, x, y) { // centre at viewport point (x, y)
  const g = gooRect();
  for (const c of d.children) { c.setAttribute("cx", x - g.left); c.setAttribute("cy", y - g.top); }
}
const ownColours = (d, on, ms = 260) => d.querySelector(".own").animate([{ opacity: on ? 1 : 0 }], { duration: ms, fill: "forwards" });
function coreSpot(size) { // a viewport rect of `size` at the centre of the big drop
  const c = centerOf($("blend-core").getBoundingClientRect());
  return { left: c.x - size / 2, top: c.y - size / 2, width: size, height: size };
}
function centerOf(r) { return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }
function flowTo(d, toRect, { scale = 1, ms = POUR_MS } = {}) {
  const from = centerOf(d.getBoundingClientRect()), to = centerOf(toRect);
  const dx = to.x - from.x, dy = to.y - from.y;
  return d.animate([
    { transform: "translate(0, 0) scale(1)" },
    { transform: `translate(${dx * .6}px, ${dy * .6}px) scale(${(1 + scale) / 2})`, offset: .6 },
    { transform: `translate(${dx}px, ${dy}px) scale(${scale})` }],
    { duration: ms, easing: "cubic-bezier(.45,0,.2,1)", fill: "forwards" }).finished;
}

// The concept: a circle at the bowl's centre, scaled by the number of trips; its gradient runs from the oldest trip's
// colour (far side) to the newest one's (the side it came from), so a new colour spreads from the point of contact.
let massDir = { x: .7, y: -.7 }, massAnim = 0;
function renderCore(n, colours) {
  const svg = $("blend-goo"), w = svg.clientWidth, h = svg.clientHeight, core = $("blend-core");
  const cx = w / 2, cy = h * .44, size = Math.min(CORE_BASE + n * 20, 164);
  core.setAttribute("cx", cx); core.setAttribute("cy", cy); core.setAttribute("r", CORE_BASE / 2);
  core.style.transform = `scale(${size / CORE_BASE})`;
  // Colours are mixed, not placed side by side: a new trip tints the whole mass (strongest on the side it came from)
  // instead of sitting in it as a pure-coloured patch that reads as a separate circle.
  const rest = "var(--card-2)", mix = (a, b, p) => `color-mix(in oklch, ${a} ${p}%, ${b})`;
  const firsts = colours.filter((_, i) => i % 2 === 0), seconds = colours.filter((_, i) => i % 2 === 1);
  const blendAll = (list) => list.reduce((acc, c, i) => (acc ? mix(c, acc, Math.round(100 / (i + 1))) : c), null);
  const all = blendAll(firsts), newest = firsts[firsts.length - 1];
  svg.style.setProperty("--m1", n ? (n > 1 ? mix(firsts[0], all, 55) : firsts[0]) : rest);
  svg.style.setProperty("--m3", n ? (n > 1 ? mix(blendAll(seconds), all, 50) : seconds[0]) : rest);
  svg.style.setProperty("--m2", n ? (n > 1 ? mix(newest, all, 60) : seconds[0]) : rest);
  aimMass(cx, cy, size / 2);
}
function aimMass(cx, cy, R) { // glide the gradient's axis to the latest contact direction
  const grad = $("mass"), get = (k) => parseFloat(grad.getAttribute(k)) || 0;
  const from = ["x1", "y1", "x2", "y2"].map(get);
  const to = [cx - massDir.x * R * 1.15, cy - massDir.y * R * 1.15, cx + massDir.x * R * 1.15, cy + massDir.y * R * 1.15];
  const t0 = performance.now(), id = ++massAnim;
  const step = (t) => {
    if (id !== massAnim) return;
    const k = Math.min(1, (t - t0) / 420), e = 1 - (1 - k) ** 3;
    ["x1", "y1", "x2", "y2"].forEach((a, i) => grad.setAttribute(a, from[i] + (to[i] - from[i]) * e));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// Flat, 2D merge: the drop glides in the plane to the rim of the big one on its own side (no tilt, no squash, no
// shrinking in flight), touches it, and melts in through the rim while the big drop grows.
function along(d, coreRect) { // measured once, before any transform: offsets put the drop's centre at `dist` from the big one
  const r = d.getBoundingClientRect(), from = centerOf(r), c = centerOf(coreRect);
  let ux = from.x - c.x, uy = from.y - c.y;
  const len = Math.hypot(ux, uy) || 1;
  ux /= len; uy /= len;
  return { R: coreRect.width / 2, rr: r.width / 2, ux, uy, at: (dist) => `translate(${c.x + ux * dist - from.x}px, ${c.y + uy * dist - from.y}px)` };
}
function approach(d, { R, rr, at }) {
  return d.animate([{ transform: "translate(0, 0)" }, { transform: at(R + rr + 4) }],
    { duration: APPROACH_MS, easing: "cubic-bezier(.4,0,.2,1)", fill: "forwards" }).finished;
}
function meltIn(d, { R, rr, at }) {
  return d.animate([
    { transform: `${at(R + rr + 4)} scale(1)` },
    { transform: `${at(R + rr * .45)} scale(.9)`, offset: .35 },
    { transform: `${at(R - rr * .1)} scale(.62)`, offset: .7 },
    { transform: `${at(R - rr * .7)} scale(.2)` }],
    { duration: 700, easing: "cubic-bezier(.3,0,.3,1)", fill: "forwards" }).finished;
}

function startDrag(e, node, id, from) {
  if (e.button !== undefined && e.button !== 0) return;
  e.preventDefault();
  const r = node.getBoundingClientRect();
  drag = { id, from, node, x0: e.clientX, y0: e.clientY, dx: e.clientX - r.left, dy: e.clientY - r.top, moving: null };
  try { node.setPointerCapture(e.pointerId); } catch (err) { /* synthetic or already released pointer */ }
  node.addEventListener("pointermove", onMove);
  node.addEventListener("pointerup", onUp, { once: true });
  node.addEventListener("pointercancel", onUp, { once: true });
}
function overBowl(x, y) {
  const r = $("blend-bowl").getBoundingClientRect();
  return x > r.left - 12 && x < r.right + 12 && y > r.top - 12 && y < r.bottom + 12;
}
function onMove(e) {
  if (!drag) return;
  if (!drag.moving) {
    if (Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < DRAG_START_PX) return;
    if (drag.from === "shelf") { // a flat bubble follows the pointer; it melts into the bowl on drop
      const r = drag.node.getBoundingClientRect();
      drag.moving = drag.node.cloneNode(true);
      drag.moving.classList.add("ghost");
      Object.assign(drag.moving.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
      document.body.append(drag.moving);
      drag.node.classList.add("lifted");
    } else { // a drop of this trip is pulled out of the mass: the goo stretches a neck between them until it tears
      drag.moving = drop(drag.id, coreSpot(44), { mass: true });
      drag.node.classList.add("lifted");
    }
  }
  if (drag.from === "shelf") {
    drag.moving.style.left = `${e.clientX - drag.dx}px`;
    drag.moving.style.top = `${e.clientY - drag.dy}px`;
    $("blend-bowl").classList.toggle("hot", overBowl(e.clientX, e.clientY));
  } else {
    moveDrop(drag.moving, e.clientX, e.clientY);
    // its own colours come back as it leaves the mass, continuously with the distance
    const core = $("blend-core").getBoundingClientRect(), c = centerOf(core);
    const k = Math.max(0, Math.min(1, (Math.hypot(e.clientX - c.x, e.clientY - c.y) - core.width / 2) / 44));
    drag.moving.querySelector(".own").setAttribute("opacity", k);
  }
}
async function onUp(e) {
  const d = drag; drag = null;
  if (!d) return;
  d.node.removeEventListener("pointermove", onMove);
  $("blend-bowl").classList.remove("hot");
  const inside = overBowl(e.clientX, e.clientY);
  if (!d.moving) { // a tap: toggle
    return d.from === "shelf" ? addTrip(d.id, d.node.getBoundingClientRect()) : removeTrip(d.id);
  }
  if (d.from === "shelf") {
    const r = d.moving.getBoundingClientRect();
    d.moving.remove();
    if (inside) return addTrip(d.id, r);
    const back = drop(d.id, r); // missed the bowl: it rolls back to the shelf
    d.node.classList.add("lifted");
    await flowTo(back, d.node.getBoundingClientRect(), { ms: 420 });
    back.remove(); d.node.classList.remove("lifted");
    return;
  }
  if (inside) { // let go inside the bowl: the drop sinks back into the mass
    ownColours(d.moving, false, 200);
    await flowTo(d.moving, $("blend-core").getBoundingClientRect(), { scale: .4, ms: 420 });
    d.moving.remove(); d.node.classList.remove("lifted");
    return;
  }
  return removeTrip(d.id, d.moving);
}

async function addTrip(id, fromRect) {
  if (B.inBowl.includes(id) || B.pending.has(id)) return;
  const loading = essence(id);
  const start = fromRect || document.querySelector(`#blend-shelf .bub[data-id="${id}"]`)?.getBoundingClientRect();
  B.pending.add(id);
  renderBlend(); // the shelf shows an empty slot, the bowl does not have it yet
  let d = null, melting = Promise.resolve(), touching = Promise.resolve();
  if (start) {
    d = drop(id, start);
    const path = along(d, $("blend-core").getBoundingClientRect());
    massDir = { x: path.ux, y: path.uy }; // the new colour will spread from this side
    touching = approach(d, path).then(() => { melting = meltIn(d, path); });
    await sleep(APPROACH_MS * .55);
    ownColours(d, false, 340); // just before contact the drop takes on the mass gradient: one body when they touch
    await sleep(APPROACH_MS * (TOUCH_AT - .55));
  }
  try { await loading; }
  catch (e) { d?.remove(); B.pending.delete(id); renderBlend(); return toast("Не получилось открыть поездку: " + e.message); }
  B.pending.delete(id);
  B.inBowl.push(id);
  saveBlend(); renderBlend(); // on contact: the colours start to mix and the big drop starts to grow while this one melts in
  await touching;
  await melting;
  d?.remove();
}

async function removeTrip(id, freeDrop) {
  const d = freeDrop || drop(id, coreSpot(44), { mass: true });
  B.inBowl = B.inBowl.filter((x) => x !== id);
  B.leaving.add(id);
  saveBlend(); renderBlend(); // the big drop shrinks while the small one travels home
  if (!freeDrop) setTimeout(() => ownColours(d, true, 300), 150);
  else ownColours(d, true, 200);
  const slot = document.querySelector(`#blend-shelf [data-id="${id}"]`);
  if (slot) await flowTo(d, slot.getBoundingClientRect(), { scale: slot.getBoundingClientRect().width / d.getBoundingClientRect().width, ms: 520 });
  d.remove();
  B.leaving.delete(id);
  renderBlend();
}

// ---------------------------------------------------------------- apply
async function applyBlend() {
  const ms = mixed().filter((m) => m.active);
  const picks = ms.filter((m) => m.group !== "context").map((m) => ({ key: m.key, source: m.pick.sources[0] }));
  const phrases = ms.filter((m) => m.group === "context").map((m) => m.pick.phrase).filter(Boolean);
  const go = $("blend-go");
  go.disabled = true;
  try {
    await api(`/api/v1/history/chats/${S.chat}/blend`, { method: "POST", body: { picks } });
  } catch (e) { toast("Не удалось сохранить концепт: " + e.message); go.disabled = false; return; }
  B.applied = { count: picks.length, titles: B.inBowl.map(tripTitle) };
  B.open = false;
  saveBlend(); renderBlend();
  window.renderMessages?.();
  const input = $("input");
  const lead = `Новая поездка с вкусами из ${B.applied.titles.map((t) => `«${short(t)}»`).join(" и ")}`;
  input.value = `${lead}${phrases.length ? ", " + phrases.join(", ") : ""}. Куда: `;
  input.dispatchEvent(new Event("input"));
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  toast("Вкусы сохранены: агент учтёт их со следующим сообщением");
}

// ---------------------------------------------------------------- open / close
async function toggleBlend(force) {
  B.open = force ?? !B.open;
  if (B.open) {
    try { B.trips = (await api("/api/v1/history/trips")).filter((t) => t.chat_id !== S.chat); }
    catch (e) { toast("Не удалось загрузить поездки: " + e.message); B.open = false; }
    loadBlend();
    await Promise.all(B.inBowl.map((id) => essence(id).catch(() => null)));
  }
  renderBlend();
}

function initBlend() {
  $("blend-btn").addEventListener("click", () => toggleBlend());
  $("blend-close").addEventListener("click", () => toggleBlend(false));
  $("blend-pill-edit").addEventListener("click", () => toggleBlend(true));
  $("blend-go").addEventListener("click", applyBlend);
  // Escape closes the panel wherever the focus is (the header button used to swallow it); the modal has its own handler
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && B.open && $("modal").classList.contains("hidden")) { toggleBlend(false); $("blend-btn").focus(); }
  });
  $("blend").addEventListener("keydown", (e) => {
    const b = e.target.closest?.(".bub, .tok");
    if (b && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      b.classList.contains("tok") ? removeTrip(b.dataset.id) : addTrip(b.dataset.id, b.getBoundingClientRect());
    }
  });
  B.trips = [];
  loadBlend();
  renderConceptPill();
}

initBlend(); // scripts sit at the end of <body>: the markup is already there
