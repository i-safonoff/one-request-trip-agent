"use strict";

// Placeholder shown when a photo URL is missing or fails to load.
const PLACEHOLDER = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="500"><rect width="100%" height="100%" fill="#cbd5e1"/>' +
  '<text x="50%" y="50%" fill="#64748b" font-family="sans-serif" font-size="28" text-anchor="middle">Нет фото</text></svg>');
const STATUS = { draft: "черновик", quoted: "цены получены", partially_quoted: "цены частично", not_quoted: "без цен", booked: "забронировано" };
const ICON = { stay: "🏨", transport: "✈️" };

const tg = window.Telegram && window.Telegram.WebApp; // present inside Telegram; initData is empty elsewhere
const planParam = new URLSearchParams(location.search).get("plan_id"); // a plan uuid or "latest"
let authToken = null;
let mapApi = null; // see map-adapters.js
let trip = null;
let filter = "all"; // "all" or a root place id
let renderSeq = 0;  // bumps on every render so late road-route responses for an old view are dropped
const routeInfo = new Map();  // step id -> { distance_m, duration_s }, filled as routes arrive
const routeCache = new Map(); // request key -> Promise<route>
const ROAD_MODES = { drive: "на машине", walk: "пешком", bike: "на велосипеде" };

const $ = (id) => document.getElementById(id);

// The plan may come from an LLM or scraper: never insert its values as HTML.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.append(c);
  return node;
}

function safeUrl(u) {
  try {
    const url = new URL(u, location.href);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function photo(src) {
  const img = el("img", { alt: "", loading: "lazy" });
  img.src = (typeof src === "string" && safeUrl(src)) || PLACEHOLDER;
  img.addEventListener("error", () => { img.src = PLACEHOLDER; }, { once: true });
  return img;
}

function money(n, cur) {
  if (n == null) return "—";
  try {
    return new Intl.NumberFormat("ru-RU", { style: "currency", currency: cur || "RUB", maximumFractionDigits: 0 }).format(n);
  } catch { return `${n} ${cur || ""}`.trim(); }
}
const fmtDate = (iso) => iso ? new Date(iso + "T00:00:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) : null;
const plural = (n, one, few, many) =>
  `${n} ${n % 10 === 1 && n % 100 !== 11 ? one : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? few : many}`;

// ---- data helpers ----
const place = (id) => trip.places[id];
const placeName = (id) => (id && place(id)?.name) || null;
const hasCoords = (p) => p && p.lat != null && p.lon != null;
// A place nested in another (airport inside a city) belongs to the top-level one.
function root(id) {
  const seen = new Set();
  while (place(id)?.parent && !seen.has(id)) { seen.add(id); id = place(id).parent; }
  return id;
}
const stepPlaces = (s) => [s.place, s.from, s.to].filter(Boolean);
const offer = (s) => s.offer || null;
const offerTitle = (o) => o && (o.name || o.title);
const offerPrice = (s) => {
  const o = offer(s);
  if (!o) return null;
  if (o.price_total != null) return o.price_total;
  if (o.price != null) return o.price;
  if (o.price_per_night != null && s.nights) return o.price_per_night * s.nights;
  return null;
};
const curOf = (s) => offer(s)?.currency || trip.budget.currency;

function stepTitle(s) {
  if (s.type === "stay") return offerTitle(offer(s)) || `Проживание: ${placeName(s.place) || "место не выбрано"}`;
  if (s.from || s.to) {
    const ot = offerTitle(offer(s));
    return `${placeName(s.from) || "?"} → ${placeName(s.to) || "?"}${ot ? " · " + ot : ""}`;
  }
  return offerTitle(offer(s)) || s.type;
}

function stepDates(s) {
  if (s.check_in || s.check_out) return `${fmtDate(s.check_in) || "?"} – ${fmtDate(s.check_out) || "?"}` + (s.nights ? ` · ${plural(s.nights, "ночь", "ночи", "ночей")}` : "");
  if (s.departure_date) return fmtDate(s.departure_date);
  return "даты не выбраны";
}

// Point on the map for a step: the chosen hotel if it has coordinates, else the place itself.
function stepPoint(s) {
  const o = offer(s);
  if (o && o.lat != null && o.lon != null) return { lat: o.lat, lon: o.lon, exact: true };
  const p = s.place && place(s.place);
  return hasCoords(p) ? { lat: p.lat, lon: p.lon, exact: false } : null;
}

const visible = (s) => filter === "all" || stepPlaces(s).some((id) => root(id) === filter);

// ---- loading / header ----
// API paths are relative to /trip-map/ui/, so the module works under whatever prefix the host mounts it at.
// Mini App sign-in: Telegram's signed initData is exchanged for a session token (the backend verifies the signature).
async function authenticate() {
  if (authToken || !tg || !tg.initData) return authToken;
  const res = await fetch("/api/v1/auth/telegram", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ init_data: tg.initData }),
  });
  if (res.ok) authToken = (await res.json()).token;
  return authToken;
}

async function init({ forceFallback = false } = {}) {
  if (tg) { tg.ready(); tg.expand(); }
  const cfg = await fetch("../api/config").then((r) => r.json());
  // ?lang=en on the page URL overrides the server default for the map labels
  const lang = new URLSearchParams(location.search).get("lang");
  if (lang && /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,4})?$/.test(lang)) cfg.language = lang;
  if (forceFallback) cfg.provider = "leaflet";

  $("map").replaceChildren();
  const { api, note } = await MapAdapters.create($("map"), cfg, {
    // Google rejects a bad/unrestricted-for-this-host key asynchronously: start over on OSM
    onAuthFail: () => init({ forceFallback: true }),
  });
  mapApi = api;
  const text = forceFallback ? "Google Maps отклонил ключ (проверьте ключ и ограничения по referrer), показана карта OpenStreetMap." : note;
  $("map-note").textContent = text || "";
  $("map-note").classList.toggle("hidden", !text);
  await load();
}

function showProblem(title, detail) {
  $("title").textContent = title;
  $("summary").replaceChildren(detail || "");
  for (const id of ["budget", "tabs", "stays"]) $(id).replaceChildren();
  $("questions").classList.add("hidden");
  if (mapApi) mapApi.clear();
}

async function load() {
  const headers = {};
  if (planParam) {
    await authenticate();
    if (!authToken) return showProblem("Откройте карту из Telegram", "Для вашей поездки нужен вход через бота.");
    headers.Authorization = "Bearer " + authToken;
  }
  const url = "../api/trip" + (planParam ? "?plan_id=" + encodeURIComponent(planParam) : "");
  const res = await fetch(url, { cache: "no-store", headers });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    if (res.status === 404 && planParam === "latest") return showProblem("Плана пока нет", detail);
    return showProblem("Не удалось загрузить план", typeof detail === "string" ? detail.slice(0, 300) : "Ошибка " + res.status);
  }
  trip = await res.json();
  if (filter !== "all" && !place(filter)) filter = "all";
  render();
}

function render() {
  renderHeader();
  renderTabs();
  renderMapAndList();
}

function renderHeader() {
  $("title").textContent = trip.title || trip.trip_id || "План поездки";
  const sum = $("summary");
  sum.replaceChildren();
  if (trip.status) sum.append(el("span", { class: `badge ${trip.status}`, text: STATUS[trip.status] || trip.status }));
  const party = trip.party || {};
  const kids = (party.children_ages || []).length;
  const who = [party.adults ? plural(party.adults, "взрослый", "взрослых", "взрослых") : null, kids ? plural(kids, "ребёнок", "ребёнка", "детей") : null].filter(Boolean).join(", ");
  if (who) sum.append(" " + who);

  const cur = trip.budget.currency;
  const b = $("budget");
  b.replaceChildren();
  const total = trip.pricing.total;
  if (total != null) b.append("Итого: ", el("b", { text: money(total, cur) }));
  else b.append("Цена ещё не рассчитана");
  if (trip.budget.max_total != null) b.append(` из ${money(trip.budget.max_total, cur)}`);
  b.classList.toggle("over", total != null && trip.budget.max_total != null && total > trip.budget.max_total);

  const q = $("questions");
  q.replaceChildren(...(trip.pending_questions || []).map((x) =>
    el("li", { text: typeof x === "string" ? x : (x.text || x.question || JSON.stringify(x)) })));
  q.classList.toggle("hidden", !q.children.length);
}

function renderTabs() {
  const tabs = $("tabs");
  tabs.replaceChildren();
  const roots = [...new Set(trip.steps.flatMap(stepPlaces).map(root))].filter((id) => placeName(id));
  if (roots.length < 2) return; // one destination: nothing to filter
  for (const key of ["all", ...roots]) {
    tabs.append(el("button", {
      type: "button", text: key === "all" ? "Все" : placeName(key), class: key === filter ? "active" : "",
      onclick: () => { filter = key; renderTabs(); renderMapAndList(); },
    }));
  }
}

// ---- map + list ----
// Road geometry from the backend (which caches and talks to the routing service).
function fetchRoute(a, b, mode) {
  const q = new URLSearchParams({ from_lat: a.lat, from_lon: a.lon, to_lat: b.lat, to_lon: b.lon, mode });
  const key = q.toString();
  if (!routeCache.has(key)) {
    const p = fetch("../api/route?" + key).then((r) => {
      if (!r.ok) throw new Error("route " + r.status);
      return r.json();
    });
    p.catch(() => routeCache.delete(key)); // failures aren't remembered, a refresh retries
    routeCache.set(key, p);
  }
  return routeCache.get(key);
}

function routeText(r, mode) {
  const km = r.distance_m / 1000;
  const mins = Math.round(r.duration_s / 60);
  const dur = mins >= 60 ? `${Math.floor(mins / 60)} ч ${mins % 60} мин` : `${mins} мин`;
  return `${km.toFixed(km < 10 ? 1 : 0)} км, ${dur} ${ROAD_MODES[mode] || ""}`.trim();
}

function renderMapAndList() {
  const seq = ++renderSeq;
  mapApi.clear();
  const list = $("stays");
  list.replaceChildren();
  const pts = [];     // every drawn point
  const pinPts = [];  // only stops (stays): the view fits these so a long-haul flight doesn't zoom out to the whole world

  // Pin numbers follow the whole trip, so a filtered view keeps the same numbers.
  let n = 0;
  trip.steps.forEach((s) => {
    const pt = s.place ? stepPoint(s) : null;
    const num = pt ? ++n : null;
    if (!visible(s)) return;

    if (pt) {
      pts.push([pt.lat, pt.lon]);
      pinPts.push([pt.lat, pt.lon]);
      mapApi.addPin(pt.lat, pt.lon, MapAdapters.pinHtml(num, pt.exact ? "" : "ghost"), stepTitle(s), () => openModal(s));
    }
    const o = offer(s);
    const baseMeta = stepDates(s) + (s.local ? " · внутри города" : "");
    const dateEl = el("div", { class: "card-meta", text: baseMeta });

    // A line between two distinct places. Road modes follow real roads (with the straight line as
    // fallback if routing is unavailable); everything else is a great-circle line. Movement inside
    // a city is only listed, unless it is a road move (e.g. airport -> city centre).
    const a = s.from && place(s.from), b = s.to && place(s.to);
    const road = s.mode in ROAD_MODES;
    if (hasCoords(a) && hasCoords(b) && (a.lat !== b.lat || a.lon !== b.lon) && (!s.local || road)) {
      pts.push([a.lat, a.lon], [b.lat, b.lon]);
      const style = { color: s.offer ? "#2563eb" : "#94a3b8", dashed: !s.offer };
      const open = () => openModal(s);
      const straight = () => mapApi.addLine([[a.lat, a.lon], [b.lat, b.lon]], { ...style, geodesic: true }, open);
      if (!road) straight();
      else fetchRoute(a, b, s.mode).then((r) => {
        if (seq !== renderSeq) return;
        routeInfo.set(s.id, r);
        dateEl.textContent = `${baseMeta} · ${routeText(r, s.mode)}`;
        mapApi.addLine(r.points, { ...style, geodesic: false }, open);
      }).catch(() => { if (seq === renderSeq) straight(); });
    }

    list.append(el("li", { class: o ? "" : "empty", onclick: () => { focus(s); openModal(s); } },
      o?.photos?.[0] || s.type === "stay" ? photo(o?.photos?.[0]) : el("div", { class: "ico", text: ICON[s.type] || "•" }),
      el("div", {},
        el("div", { class: "card-name", text: (num ? `${num}. ` : "") + stepTitle(s) }),
        dateEl,
        el("div", { class: "card-meta", text: o ? (o.rating != null ? `★ ${o.rating.toFixed(1)} · ` : "") + money(offerPrice(s), curOf(s)) : "вариант не выбран" }),
      )));
  });

  mapApi.fit(pinPts.length ? pinPts : pts);
}

function focus(s) {
  const pt = s.place ? stepPoint(s) : null;
  if (pt) mapApi.focusOn(pt.lat, pt.lon, 13);
  else {
    const pts = [s.from, s.to].map((id) => id && place(id)).filter(hasCoords).map((p) => [p.lat, p.lon]);
    if (pts.length) mapApi.fit(pts);
  }
}

// ---- modal ----
function openModal(s) {
  const o = offer(s) || {};
  const cur = curOf(s);
  const photos = Array.isArray(o.photos) && o.photos.length ? o.photos : null;
  const row = (k, v) => v == null || v === "" ? null :
    el("div", { class: "row" }, el("span", { class: "k", text: k }), el("span", { text: v }));

  let gallery = null;
  if (photos) {
    let idx = 0, img = photo(photos[0]);
    const count = el("span", { class: "count", text: `1 / ${photos.length}` });
    const show = (d) => {
      idx = (idx + d + photos.length) % photos.length;
      const next = photo(photos[idx]);
      img.replaceWith(next); // fresh element so the error fallback re-arms
      img = next;
      count.textContent = `${idx + 1} / ${photos.length}`;
    };
    gallery = el("div", { class: "gallery" }, img);
    if (photos.length > 1) gallery.append(
      el("button", { class: "nav prev", type: "button", text: "‹", "aria-label": "Назад", onclick: () => show(-1) }),
      el("button", { class: "nav next", type: "button", text: "›", "aria-label": "Вперёд", onclick: () => show(1) }),
      count);
  }

  const isStay = s.type === "stay";
  const info = el("div", { class: "modal-info" },
    el("h2", { text: stepTitle(s) }),
    o.rating != null || o.stars ? el("div", {},
      o.rating != null ? el("span", { class: "rating", text: o.rating.toFixed(1) }) : null,
      o.stars ? el("span", { class: "stars", text: "★".repeat(o.stars) }) : null,
      o.reviews_count ? el("span", { class: "card-meta", text: ` ${o.reviews_count} отзывов` }) : null) : null,
    o.address ? el("div", { class: "card-meta", text: o.address }) : null,
    Array.isArray(o.amenities) && o.amenities.length ? el("div", { class: "chips" }, ...o.amenities.map((a) => el("span", { text: String(a) }))) : null,
    s.offer ? null : el("p", { class: "card-meta", text: isStay ? "Отель ещё не выбран." : "Вариант ещё не выбран." }),
    isStay ? row("Заезд", fmtDate(s.check_in) || "не выбран") : row("Дата", fmtDate(s.departure_date) || "не выбрана"),
    isStay ? row("Выезд", fmtDate(s.check_out) || "не выбран") : null,
    isStay && s.nights ? row("Ночей", String(s.nights)) : null,
    routeInfo.has(s.id) ? row("В пути", routeText(routeInfo.get(s.id), s.mode)) : null,
    o.price_per_night != null ? row("За ночь", money(o.price_per_night, cur)) : null,
    s.offer ? row("Итого", money(offerPrice(s), cur)) : null,
    o.details ? el("p", { class: "card-meta", text: o.details }) : null,
    safeUrl(o.url) ? el("a", { class: "btn", href: safeUrl(o.url), target: "_blank", rel: "noopener noreferrer", text: "Открыть" }) : null,
  );

  $("modal-body").replaceChildren(...[gallery, info].filter(Boolean));
  $("modal").classList.remove("hidden");
}

const closeModal = () => $("modal").classList.add("hidden");
$("modal-close").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
$("reload").addEventListener("click", load);

init();
