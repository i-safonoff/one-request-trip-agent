"use strict";
// Chat + plan + map UI. Works at http://localhost:8080 (guest session) and as a Telegram Mini App (initData).
// Plan data comes from an LLM pipeline and external sources: values are only ever inserted via textContent.

const $ = (id) => document.getElementById(id);
const S = { token: localStorage.getItem("ekv_token"), chat: localStorage.getItem("ekv_chat"), plan: null, planId: null, rev: null, tab: "chat", messages: [], progress: null, busy: false };
const tg = window.Telegram && window.Telegram.WebApp;

// ---------------------------------------------------------------- theme
// index.html sets data-theme before the first paint; the button flips it and remembers the choice.
// Without a saved choice the page keeps following the system theme.
function setTheme(theme, save) {
  document.documentElement.dataset.theme = theme;
  const b = $("theme-toggle");
  if (b) { b.setAttribute("aria-pressed", String(theme === "dark")); b.setAttribute("aria-label", theme === "dark" ? "Светлая тема" : "Тёмная тема"); }
  if (save) try { localStorage.setItem("ekv_theme", theme); } catch (e) { /* private mode: the choice lasts this visit */ }
}
function initThemeToggle() {
  setTheme(document.documentElement.dataset.theme || "light", false);
  $("theme-toggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    let saved = null;
    try { saved = localStorage.getItem("ekv_theme"); } catch (err) { /* ignore */ }
    if (!saved) setTheme(e.matches ? "dark" : "light", false);
  });
}

// ---------------------------------------------------------------- helpers
function el(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) n.append(c);
  return n;
}
const money = (v, cur = "RUB") => {
  if (v == null) return "—";
  try { return new Intl.NumberFormat("ru-RU", { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(Number(v)); }
  catch { return `${v} ${cur}`; }
};
// Times from the server carry the place's own UTC offset ("2026-11-05T12:00:00+03:00"). They are shown as written
// there (hotel / station local time), never converted to the viewer's device zone: the text the agent writes uses the same wall clock.
const wall = (iso, opts) => {
  const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)/.exec(iso || "");
  if (!m) return iso || "";
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])).toLocaleString("ru-RU", { timeZone: "UTC", ...opts });
};
const fmtDate = (iso) => iso ? new Date(iso + "T00:00:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) : null;
const dayRange = (a, b) => a && b ? `${fmtDate(a)} – ${fmtDate(b)}` : (fmtDate(a) || "даты не указаны");
const safeUrl = (u) => { try { const x = new URL(u, location.href); return ["http:", "https:"].includes(x.protocol) ? x.href : null; } catch { return null; } };
const STATUS = { draft: ["черновик", ""], searching: ["ищу…", ""], needs_input: ["нужны уточнения", "warn"], choosing_destination: ["выберите направление", "warn"],
  ready: ["готово", "ok"], partial: ["неполный план", "warn"], in_cart: ["в корзине", "ok"], booked: ["оформлено", "ok"] };
const ICON = { stay: "🏨", transport: "🚆", transfer: "🚕" };
const MODE = { rail: "🚆", flight: "✈️", bus: "🚌", drive: "🚗" };
const COND = { breakfast: "завтрак", free_cancellation: "бесплатная отмена", pets_allowed: "с животными", kid_friendly: "для детей",
  min_stars: "звёзды", min_rating: "рейтинг", max_travel_hours: "время в пути", max_transfers: "пересадки", avoid_overnight: "без ночного" };

function toast(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

// FastAPI answers a failed validation with a list of {type, loc, msg, ctx}: show it in words, not as JSON
const FIELD_RU = { name: "Имя", email: "Email", text: "Сообщение", answer: "Ответ" };
function humanDetail(d) {
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d.map((x) => {
      const f = FIELD_RU[x.loc?.[x.loc.length - 1]] || "Поле", c = x.ctx || {};
      if (x.type === "string_too_short") return `${f}: минимум ${c.min_length} симв.`;
      if (x.type === "string_too_long") return `${f}: максимум ${c.max_length} симв.`;
      if (x.type === "missing") return `${f}: заполните поле`;
      if (/email/.test(x.type || "") || /email/i.test(x.msg || "")) return `${f}: проверьте адрес`;
      return `${f}: проверьте значение`;
    }).join("; ");
  }
  return d?.message || "Не удалось выполнить запрос";
}

async function api(path, { method = "GET", body, retry = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (S.token) headers.Authorization = `Bearer ${S.token}`;
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined, credentials: "same-origin" });
  if (r.status === 401 && retry) { await authenticate(); return api(path, { method, body, retry: false }); }
  if (!r.ok) {
    let detail = r.statusText;
    try { const j = await r.json(); detail = humanDetail(j.detail); } catch { /* not json */ }
    const e = new Error(detail); e.status = r.status; throw e;
  }
  return r.status === 204 ? null : r.json();
}

// ---------------------------------------------------------------- session
async function authenticate() {
  let res = null;
  if (tg && tg.initData) {
    try { res = await fetch("/api/v1/auth/telegram", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ init_data: tg.initData }) }).then((r) => r.ok ? r.json() : null); }
    catch { /* fall through to guest */ }
  }
  if (!res) res = await fetch("/api/v1/auth/guest", { method: "POST", credentials: "same-origin" }).then((r) => r.json());
  S.token = res.token; localStorage.setItem("ekv_token", S.token);
}

async function ensureChat(forceNew = false) {
  // the chat module owns chats: GET /chats/active (created if missing), POST /chats starts a new trip
  const c = forceNew ? await api("/api/v1/chats", { method: "POST" }) : await api("/api/v1/chats/active");
  S.chat = c.id;
  localStorage.setItem("ekv_chat", S.chat);
}

// ---------------------------------------------------------------- chat
function renderMessages() {
  window.renderConceptPill?.(); // blend.js: the concept pill belongs to the current chat
  const box = $("messages");
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 80;
  box.replaceChildren();
  if (!S.messages.length) {
    box.append(el("div", { class: "msg assistant", text: "Привет! Опишите поездку своими словами: куда, когда, с кем и сколько готовы потратить. Если чего-то не хватит, спрошу один раз и коротко." }));
  }
  const lastAssistant = [...S.messages].reverse().find((m) => m.role === "assistant");
  for (const m of S.messages) {
    const bubble = el("div", { class: `msg ${m.role}${m.turn?.kind === "error" ? " error" : ""}` });
    if (m.role === "assistant") bubble.append(...answerLines(m.text)); else bubble.textContent = m.text;
    if (m.role === "assistant" && m.turn) renderTurnDetails(bubble, m.turn, m === lastAssistant);
    box.append(bubble);
  }
  if (stick) box.scrollTop = box.scrollHeight;
  renderReplies(lastAssistant);
}

// The agent's answer is built by code as short lines: "• pick", indented "— fact", "⚠ risk". One element per line keeps the
// structure readable (the text is only ever inserted via textContent).
// A "•" line is a heading only when indented lines follow it (the template answer: "• Hotel" + "  — why"). An answer
// written as a plain list of "•" lines (typical of the LLM) stays regular text with a hanging bullet, not all bold.
function answerLines(text) {
  const raws = String(text || "").split("\n");
  const indented = (r) => /^\s{2,}\S/.test(r || "");
  return raws.map((raw, i) => {
    const t = raw.trim();
    if (!t) return el("div", { class: "gap" });
    const bullet = t.startsWith("•");
    const kind = indented(raw) ? "sub" : bullet && indented(raws[i + 1]) ? "lead" : bullet ? "bullet" : "";
    const cls = ["line", kind, t.startsWith("⚠") ? "risk" : ""].filter(Boolean).join(" ");
    return el("div", { class: cls, text: kind === "bullet" ? t.replace(/^•\s*/, "") : t });
  });
}

function renderTurnDetails(bubble, turn, isLast) {
  // one compact "why" block for the whole answer instead of a cut-off summary line per recommendation
  const recs = (turn.recommendations || []).filter((r) => r.why?.length || r.tradeoffs?.length || r.risks?.length);
  if (recs.length) {
    const d = el("details", { class: "why" }, el("summary", { text: recs.length === 1 ? "Почему такой выбор" : `Почему такой выбор · ${recs.length}` }));
    for (const r of recs) {
      const sec = el("section", {}, el("p", { class: "why-title", text: r.title }));
      const items = [...(r.why || []).map((w) => el("li", { text: w })),
        ...(r.tradeoffs || []).slice(0, 1).map((t) => el("li", { class: "trade", text: `По сравнению с «${t.vs_title}»: ${t.text}` })),
        ...(r.risks || []).map((k) => el("li", { class: "risk", text: k }))];
      if (items.length) sec.append(el("ul", {}, items));
      d.append(sec);
    }
    bubble.append(d);
  }
  if (turn.changes?.length) {
    bubble.append(el("div", { class: "changes" }, el("strong", { text: "Что изменилось" }),
      el("ul", {}, turn.changes.map((c) => el("li", { text: `${c.before || "—"} → ${c.after || "—"}${c.delta ? ` (${Number(c.delta) > 0 ? "+" : ""}${money(c.delta)})` : ""}` })))));
  }
  if (turn.kind === "destinations" && isLast) bubble.append(destinationCards());
}

function destinationCards() {
  const wrap = el("div", { class: "dest-cards" });
  for (const c of S.plan?.destination_candidates || []) {
    wrap.append(el("div", { class: "dest" }, el("h4", { text: `${c.name}, ${c.country || ""}` }),
      el("ul", {}, (c.why || []).map((w) => el("li", { text: w }))),
      c.facts?.["ориентир_цены"] ? el("div", { class: "muted", text: c.facts["ориентир_цены"] + (c.price_hint_mode === "mock" ? " (демо-данные)" : "") }) : null,
      el("button", { class: "primary", type: "button", text: `Выбрать ${c.name}`, onclick: () => act(() => api(`/api/v1/chats/${S.chat}/destinations/${encodeURIComponent(c.key)}/choose`, { method: "POST" })) })));
  }
  return wrap;
}

function renderReplies(lastAssistant) {
  const box = $("replies");
  box.replaceChildren();
  if (S.busy || !lastAssistant?.turn) return;
  const t = lastAssistant.turn;
  for (const q of t.kind === "destinations" ? [] : t.questions || []) for (const o of q.options || []) {
    box.append(el("button", { type: "button", text: o, onclick: () => sendAnswer(q.id, o) }));
  }
  const payable = S.plan && ["ready", "in_cart"].includes(S.plan.status) && S.plan.pricing?.is_complete !== false
    && !(S.plan.checks || []).some((c) => c.severity === "error");
  for (const r of t.kind === "destinations" ? [] : t.quick_replies || []) { // destinations: the cards carry their own "Выбрать" buttons
    if (r === "Оформить" && !payable) continue;
    box.append(el("button", { type: "button", text: r, onclick: () => (r === "Оформить" ? openCart() : send(r)) }));
  }
}

async function loadMessages() {
  S.messages = await api(`/api/v1/chats/${S.chat}/messages`);
  renderMessages();
}

async function act(fn) {
  try { await fn(); setBusy(true); } catch (e) { toast(e.message); }
}
const send = (text) => act(() => api(`/api/v1/chats/${S.chat}/messages`, { method: "POST", body: { text, client_message_id: crypto.randomUUID() } })
  .then(() => { S.messages.push({ role: "user", text }); renderMessages(); }));
const sendAnswer = (qid, answer) => act(() => api(`/api/v1/chats/${S.chat}/clarifications/${qid}/answers`, { method: "POST", body: { answer } })
  .then(() => { S.messages.push({ role: "user", text: answer }); renderMessages(); }));

let busyTimer = null;
function setBusy(b, text) {
  S.busy = b;
  clearTimeout(busyTimer);
  // no progress for a long time: the worker may be down or the model slow: say so instead of spinning silently
  if (b) busyTimer = setTimeout(() => { if (S.busy) $("progress-text").textContent = `${S.progress || "Работаю над запросом…"} Это дольше обычного, подождите ещё.`; }, 60000);
  $("progress").classList.toggle("hidden", !b);
  $("progress-text").textContent = text || (b ? "Работаю над запросом…" : "");
  $("send").disabled = false;
  if (!b) renderReplies([...S.messages].reverse().find((m) => m.role === "assistant"));
  else $("replies").replaceChildren();
}

// ---------------------------------------------------------------- plan
const SOURCE_NAME = { tutu_mcp: "Туту", mock: "демо" };
const emptyPlan = () => ({ status: "draft", sequence: [], nodes: {}, offers: {}, places: {}, pricing: {}, checks: [], data_notes: [],
  questions: [], destination_candidates: [], cart: { status: "empty" }, understood: [], budget: {}, constraints: [] });

async function loadPlan() {
  let r = null;
  try { r = await api(`/api/v1/chats/${S.chat}/plan`); } catch (e) { if (e.status !== 404) throw e; } // 404: no plan yet
  S.plan = r ? { ...emptyPlan(), ...r.view, pricing: { ...emptyPlan().pricing, ...(r.view.pricing || {}) } } : emptyPlan();
  S.planId = r ? r.plan_id : null;
  const rev = r ? r.revision : null;
  if (rev != null && S.rev != null && rev !== S.rev && S.tab !== "plan") document.querySelector('#tabs [data-tab="plan"]').classList.add("dot"); // mobile: the plan is on another tab
  S.rev = rev;
  const st = await api(`/api/v1/chats/${S.chat}/status`);
  S.plan.busy = st.busy;
  if (S.plan.busy !== S.busy && !S.progress) setBusy(S.plan.busy);
  renderPlan(); renderMap();
}

function constraintChips(opt) {
  return Object.entries(opt?.constraints || {}).map(([k, v]) => el("span", { class: `chip ${v}`, text: `${COND[k] || k}: ${v === "pass" ? "да" : v === "fail" ? "нет" : "не подтверждено"}` }));
}

function renderPlan() {
  const p = S.plan, body = $("plan-body");
  body.replaceChildren();
  $("plan-foot").replaceChildren();  // a checkout button of an earlier plan must not outlive it
  if (tg && tg.MainButton) tg.MainButton.hide();
  const [st, cls] = STATUS[p.status] || [p.status, ""];
  $("plan-status").textContent = st; $("plan-status").className = `badge ${cls}`;
  $("plan-title").textContent = p.title || "План поездки";
  if (!p.sequence.length) {
    body.append(el("p", { class: "muted empty-note", text: p.questions?.length ? "Жду ответы на уточняющие вопросы в чате." : "Здесь появится план, когда агент поймёт, что вам нужно." }));
    return;
  }
  const pr = p.pricing, over = pr.within_budget === false;
  const summary = el("div", { class: "summary" },
    el("div", { class: `total${over ? " over" : ""}`, text: pr.is_complete ? money(pr.total, pr.currency) : `от ${money(pr.known_subtotal, pr.currency)}` }),
    el("div", { class: "muted", text: pr.is_complete ? `Итого${pr.excluded?.length ? ", без трансферов" : ""}` : "Расчёт неполный: не все части подобраны" }),
    el("div", { class: "facts" }, (p.understood || []).map((u) => el("span", { text: u.value }))));
  if (p.budget?.amount) summary.append(el("div", { class: over ? "over" : "muted", text: `Бюджет: ${money(p.budget.amount, p.budget.currency)}${over ? " — превышен" : ""}` }));
  body.append(summary);

  const mocks = Object.values(p.offers).filter((o) => o.data_mode === "mock").length;
  if (mocks) body.append(el("div", { class: "notice mock", text: `В плане есть демо-данные (${mocks}): цены и расписание сгенерированы, это не реальные предложения.` }));
  for (const n of p.data_notes || []) if (n.kind !== "mock") body.append(el("div", { class: "notice warn", text: n.text }));
  for (const c of (p.checks || []).filter((c) => c.severity === "error")) body.append(el("div", { class: "notice bad", text: c.text }));

  for (const id of p.sequence) {
    const nd = p.nodes[id], o = p.offers[nd.selected_offer_ref];
    const thumb = o?.photos?.[0] && safeUrl(o.photos[0]) ? el("img", { class: "thumb", src: safeUrl(o.photos[0]), alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : el("div", { class: "ico", text: nd.type === "transport" ? (MODE[nd.mode] || ICON.transport) : ICON[nd.type] });
    const sel = (nd.options || []).find((x) => x.offer_id === nd.selected_offer_ref);
    const dates = nd.type === "stay" ? dayRange(nd.check_in, nd.check_out) : fmtDate(nd.departure_date);
    body.append(el("div", { class: `node ${nd.status}`, role: "button", tabindex: "0", onclick: () => openNode(id),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openNode(id); } } }, thumb,
      el("div", {},
        el("div", { class: "name", text: nd.label }),
        el("div", { class: "meta", text: [dates, nd.type === "transfer" && nd.attrs?.duration_min ? `~${nd.attrs.duration_min} мин` : null].filter(Boolean).join(" · ") }),
        o ? el("div", {}, el("span", { class: "price", text: money(o.price.amount, o.price.currency) }), el("span", { class: "meta", text: ` · ${o.title}` })) : (nd.type === "transfer" ? null : el("div", { class: "meta", text: nd.status === "not_found" ? "вариантов не найдено" : "вариант не выбран" })),
        el("div", { class: "chips" },
          o?.data_mode === "mock" ? el("span", { class: "badge mock", text: "демо" }) : null,
          nd.pinned ? el("span", { class: "chip", text: "📌 закреплено" }) : null,
          nd.status === "stale" ? el("span", { class: "chip unknown", text: "нужен пересчёт" }) : null,
          constraintChips(sel)))));
  }
  // the checkout button lives in the pane's footer, outside the scrolling list, so it never covers a card
  const errors = (p.checks || []).filter((c) => c.severity === "error");
  const payable = ["ready", "in_cart"].includes(p.status) && p.pricing.is_complete !== false && !errors.length;
  // the only blocker is the shifted dates (a late flight): the user can knowingly accept them and pay
  const shift = p.pricing.is_complete !== false && errors.length === 1 && errors[0].code === "TRIP_DATES_MISMATCH" ? errors[0] : null;
  if (shift) {
    const f = shift.facts || {};
    $("plan-foot").replaceChildren(el("button", { id: "accept-dates-btn", class: "primary", type: "button",
      text: `Принять даты${f.stays_from ? ` ${f.stays_from.slice(8)}.${f.stays_from.slice(5, 7)}–${f.stays_to.slice(8)}.${f.stays_to.slice(5, 7)}` : ""}`,
      onclick: () => act(() => api(`/api/v1/chats/${S.chat}/accept-dates`, { method: "POST" })) }));
  } else $("plan-foot").replaceChildren(...(payable
    ? [el("button", { id: "cart-btn", class: "primary", type: "button", text: p.status === "in_cart" ? "Продолжить оформление" : "Оформить", onclick: openCart })] : []));
  if (p.status === "booked") body.append(el("div", { class: "notice mock", text: `Заказ ${p.cart.order_number} оформлен (оплата имитирована).` }));
  if (tg && tg.MainButton) {
    if (payable) { tg.MainButton.setText("Оформить"); tg.MainButton.onClick(openCart); tg.MainButton.show(); } else tg.MainButton.hide();
  }
}

// ---------------------------------------------------------------- modal
let lastFocus = null;
function showModal(...nodes) {
  lastFocus = document.activeElement;
  $("modal-body").replaceChildren(...nodes);
  $("modal").classList.remove("hidden");
  document.body.classList.add("modal-open");
  $("modal-close").focus();
}
function closeModal() {
  $("modal").classList.add("hidden");
  document.body.classList.remove("modal-open");
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
}
document.addEventListener("keydown", (e) => {
  if ($("modal").classList.contains("hidden")) return;
  if (e.key === "Escape") return closeModal();
  if (e.key !== "Tab") return;
  const items = [...$("modal").querySelectorAll("button, input, a[href], [tabindex]:not([tabindex='-1'])")].filter((n) => !n.disabled && n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

function offerRows(o) {
  const rows = [];
  if (o.hotel) {
    const h = o.hotel, r = o.rate || {};
    rows.push(["Рейтинг", h.rating != null ? `${h.rating} / 10${h.review_count ? ` · ${h.review_count} отзывов` : ""}` : "не указан"]);
    if (h.address) rows.push(["Адрес", h.address]);
    if (r.room_name) rows.push(["Номер", r.room_name]);
    rows.push(["Питание", r.breakfast_included ? "завтрак включён" : (r.breakfast_included === false ? "без завтрака" : "не указано")]);
    rows.push(["Отмена", r.free_cancellation ? `бесплатная${r.free_cancellation_until ? " до " + wall(r.free_cancellation_until, { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : ""}` : (r.free_cancellation === false ? "платная" : "не указана")]);
    if (h.check_in_time) rows.push(["Заезд / выезд", `с ${h.check_in_time} / до ${h.check_out_time || "—"}`]);
  }
  if (o.transport) {
    const t = o.transport;
    for (const s of t.segments) rows.push([`${MODE[s.mode] || ""} ${s.carrier || s.mode}`, `${s.origin_name} ${wall(s.departure, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} → ${s.destination_name} ${wall(s.arrival, s.arrival.slice(0, 10) === s.departure.slice(0, 10) ? { hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} (местное время)`]);
    rows.push(["В пути", `${Math.floor(t.duration_min / 60)} ч ${t.duration_min % 60} мин${t.transfers ? `, пересадок: ${t.transfers}` : ", без пересадок"}`]);
  }
  rows.push(["Цена", `${money(o.price.amount, o.price.currency)}${o.price.basis === "stay_total" ? " за всё проживание" : ""}`]);
  rows.push(["Данные", o.data_mode === "mock" ? "демо (сгенерированы)" : `${SOURCE_NAME[o.source] || o.source}, получены ${new Date(o.retrieved_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`]);
  return rows.map(([k, v]) => el("div", { class: "row" }, el("span", { class: "k", text: k }), el("span", { text: v })));
}

function openNode(id) {
  const p = S.plan, nd = p.nodes[id], o = p.offers[nd.selected_offer_ref], rec = nd.recommendation;
  const info = el("div", { class: "modal-info" }, el("h2", { text: o ? o.title : nd.label }), el("div", { class: "muted", text: nd.label }));
  const photos = (o?.photos || []).map(safeUrl).filter(Boolean).slice(0, 6);
  const nodes = [];
  if (photos.length) nodes.push(el("div", { class: "gallery" }, photos.map((u) => el("img", { src: u, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }))));
  if (o) info.append(...offerRows(o));
  if (nd.type === "transfer") {
    const a = nd.attrs || {};
    const row = (k, v) => el("div", { class: "row" }, el("span", { class: "k", text: k }), el("span", { text: v }));
    info.append(row("Расстояние", a.distance_km != null ? `около ${a.distance_km} км` : "—"), row("Время в пути", a.duration_min != null ? `около ${a.duration_min} мин` : "—"),
      el("p", { class: "muted", text: "Оценка по прямой с поправкой на дороги, не расписание и не цена такси. Стоимость трансфера в итог не входит." }));
  }
  const h = o?.hotel, aspects = Object.entries(h?.aspects || {});
  if (aspects.length) {
    info.append(el("h3", { text: "Оценки гостей по темам" }), el("div", { class: "aspects" }, aspects.map(([k, v]) =>
      el("div", { class: `aspect ${v < 7 ? "weak" : v >= 8.5 ? "strong" : ""}` }, el("span", { text: k }), el("b", { text: String(v).replace(".", ",") })))));
  }
  if (h?.pros?.length || h?.cons?.length) {
    info.append(el("h3", { text: `Что пишут гости${h.reviews_used ? ` (из ${h.reviews_used} свежих отзывов)` : ""}` }),
      el("ul", { class: "quotes" }, [...(h.pros || []).map((q) => el("li", { class: "q-pro", text: `👍 «${q}»` })), ...(h.cons || []).map((q) => el("li", { class: "q-con", text: `👎 «${q}»` }))]));
  }
  if (rec?.why?.length) info.append(el("h3", { text: "Почему это подходит" }), el("ul", {}, rec.why.map((w) => el("li", { text: w }))));
  if (rec?.tradeoffs?.length) info.append(el("h3", { text: "Сравнение с альтернативами" }), el("ul", {}, rec.tradeoffs.map((t) => el("li", { text: `«${t.vs_title}»: ${t.text}` }))));
  const risks = [...(rec?.risks || []), ...p.checks.filter((c) => c.node_ids.includes(id) && c.severity !== "info").map((c) => c.text)];
  if (risks.length) info.append(el("h3", { text: "Риски" }), el("ul", {}, [...new Set(risks)].map((r) => el("li", { class: "risk", text: `⚠ ${r}` }))));

  const opts = nd.options || [];
  if (opts.length > 1) {
    info.append(el("h3", { text: `Альтернативы (${opts.length})` }));
    const list = el("div", { class: "alts" });
    for (const op of opts) {
      const ao = p.offers[op.offer_id]; if (!ao) continue;
      const current = op.offer_id === nd.selected_offer_ref;
      list.append(el("div", { class: `alt${current ? " current" : ""}` },
        el("div", {}, el("div", { class: "t", text: ao.title }), el("div", { class: "muted", text: `${money(ao.price.amount, ao.price.currency)} · оценка ${op.score.toFixed(2)}` }),
          el("div", { class: "chips" }, constraintChips(op))),
        current ? el("span", { class: "badge ok", text: "выбрано" }) : el("button", { class: "ghost", type: "button", text: "Выбрать",
          disabled: !op.passes_hard || null, onclick: () => { closeModal(); act(() => api(`/api/v1/plans/${S.planId}/nodes/${id}/select`, { method: "POST", body: { offer_id: op.offer_id, pin: true } })); } })));
    }
    info.append(list);
  }
  if (nd.pinned) info.append(el("button", { class: "ghost", type: "button", text: "Открепить выбор", onclick: () => { closeModal(); act(() => api(`/api/v1/plans/${S.planId}/nodes/${id}/unpin`, { method: "POST" })); } }));
  showModal(...nodes, info);
  focusNode(id);
}

// ---------------------------------------------------------------- cart / checkout (simulated payment)
async function openCart() {
  const info = el("div", { class: "modal-info" }, el("h2", { text: "Оформление" }), el("p", { class: "muted", text: "Собираю корзину и перепроверяю цены у источника…" }));
  showModal(info);
  try {
    let cart = S.plan.cart?.status !== "empty" && S.plan.cart?.id ? S.plan.cart : (await api(`/api/v1/plans/${S.planId}/cart`, { method: "POST" })).cart;
    const chk = await api(`/api/v1/carts/${cart.id}/check`, { method: "POST" });
    await loadPlan();
    info.replaceChildren(el("h2", { text: "Ваш заказ" }));
    for (const i of chk.cart.items) info.append(el("div", { class: "row" }, el("span", { text: i.title }), el("strong", { text: money(i.price.amount, i.price.currency) })));
    info.append(el("div", { class: "row" }, el("span", { class: "k", text: "Итого" }), el("strong", { text: money(chk.cart.total.amount, chk.cart.total.currency) })));
    for (const c of chk.price_changes) info.append(el("div", { class: "notice warn", text: `Цена изменилась: ${c.title}: ${money(c.old, c.currency)} → ${money(c.new, c.currency)}` }));
    for (const b of chk.blockers) info.append(el("div", { class: "notice bad", text: b.text }));
    if (!chk.ready_to_pay) { info.append(el("p", { class: "muted", text: "Оплата недоступна, пока есть эти проблемы. Измените план в чате." })); return; }
    const name = el("input", { placeholder: "Имя и фамилия", autocomplete: "name", required: "", minlength: "2", maxlength: "120" });
    const email = el("input", { placeholder: "Email", type: "email", autocomplete: "email", required: "" });
    const pay = el("button", { class: "primary", type: "submit", text: `Оплатить ${money(chk.cart.total.amount, chk.cart.total.currency)} (имитация)` });
    const form = el("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault(); pay.disabled = true;
      try {
        const r = await api(`/api/v1/carts/${cart.id}/checkout`, { method: "POST", body: { guest: { name: name.value, email: email.value } } });
        info.replaceChildren(el("div", { class: "paid" }, el("div", { text: "✅ Заказ оформлен" }), el("div", { class: "num", text: r.order_number }),
          el("p", { class: "muted", text: `${money(r.total, r.currency)} · ${r.note}` })));
        await Promise.all([loadPlan(), loadMessages()]);
      } catch (err) { toast(err.message); pay.disabled = false; }
    } }, name, email, pay, el("p", { class: "muted", text: "Платёж имитируется: карта не нужна, деньги не списываются, у поставщиков ничего не бронируется." }));
    info.append(form);
  } catch (e) { info.replaceChildren(el("h2", { text: "Не получилось" }), el("p", { text: e.message }), el("button", { class: "ghost", type: "button", text: "Закрыть", onclick: closeModal })); }
}

// ---------------------------------------------------------------- map
let mapApi = null, renderSeq = 0;
const routeCache = new Map();
async function initMap() {
  const cfg = await fetch("/trip-map/api/config").then((r) => r.json());
  $("map").replaceChildren();
  const setNote = (t) => { $("map-note").textContent = t || ""; $("map-note").classList.toggle("hidden", !t); };
  const { api: m, note } = await MapAdapters.create($("map"), cfg, { onNote: setNote, onAuthFail: () => MapAdapters.create($("map"), { ...cfg, provider: "leaflet" }).then((x) => { mapApi = x.api; renderMap(); }) });
  mapApi = m;
  setNote(note);
  renderMap(); // the plan usually loads before the map is ready
}

function fetchRoute(a, b) {
  const key = [a, b].flat().join(",");
  if (!routeCache.has(key)) routeCache.set(key, fetch(`/trip-map/api/route?from_lat=${a[0]}&from_lon=${a[1]}&to_lat=${b[0]}&to_lon=${b[1]}&mode=drive`).then((r) => { if (!r.ok) throw new Error("no route"); return r.json(); }));
  return routeCache.get(key);
}

function renderMap() {
  if (!mapApi || !S.plan) return;
  const p = S.plan, seq = ++renderSeq;
  mapApi.clear();
  const pins = [], drawn = [];
  let n = 0;
  for (const id of p.sequence) {
    const nd = p.nodes[id], o = p.offers[nd.selected_offer_ref], pl = p.places[nd.place];
    if (nd.type === "stay") {
      const lat = o?.lat ?? pl?.lat, lon = o?.lon ?? pl?.lon;
      if (lat == null) continue;
      n += 1; pins.push([lat, lon]);
      mapApi.addPin(lat, lon, MapAdapters.pinHtml(n, o ? "" : "ghost"), nd.label, () => openNode(id));
      continue;
    }
    const coords = (nd.geometry?.coordinates || []).map(([lo, la]) => [la, lo]);
    if (coords.length < 2) continue;
    drawn.push({ coords, geodesic: nd.geometry_status === "great_circle" });
    const style = { color: o || nd.type === "transfer" ? routeColor() : "#94a3b8", dashed: !o && nd.type !== "transfer" };
    const open = () => openNode(id);
    const straight = () => mapApi.addLine(coords, { ...style, geodesic: nd.geometry_status === "great_circle" }, open);
    // a bus without known stations is a drive between the two cities: follow the road, not a ruler line
    const byRoad = nd.type === "transfer" || (["straight", "stations"].includes(nd.geometry_status) && o?.transport?.mode === "bus");
    if (byRoad) {
      fetchRoute(coords[0], coords[coords.length - 1]).then((r) => { if (seq === renderSeq) mapApi.addLine(r.points, { ...style, geodesic: false }, open); })
        .catch(() => { if (seq === renderSeq) straight(); });
    } else straight();
  }
  mapApi.fit(MapAdapters.routePoints(pins, drawn)); // the whole route (arcs included), start and finish too, not only the hotels
}
// lines take the theme's accent (a darker shade, so they stand out on light map tiles)
const routeColor = () => getComputedStyle(document.documentElement).getPropertyValue("--accent-deep").trim() || "#26d494";

function focusNode(id) {
  const nd = S.plan.nodes[id], o = S.plan.offers[nd.selected_offer_ref], pl = S.plan.places[nd.place];
  const lat = o?.lat ?? pl?.lat, lon = o?.lon ?? pl?.lon;
  if (mapApi && lat != null && nd.type === "stay") mapApi.focusOn(lat, lon, 12);
}

// ---------------------------------------------------------------- live events
// Server-Sent Events over fetch: EventSource cannot send headers, which forced the session token into the URL (and into
// every access log). A reconnect resumes after the last event seen, so nothing is replayed or lost.
let streamCtl = null, refreshTimer = null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const showNet = (down) => $("net").classList.toggle("hidden", !down);

function parseSse(block) {
  const ev = { id: null, event: "message", data: [] };
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue; // keep-alive comment
    const i = line.indexOf(":"), k = i < 0 ? line : line.slice(0, i), v = i < 0 ? "" : line.slice(i + 1).replace(/^ /, "");
    if (k === "id") ev.id = Number(v) || null; else if (k === "event") ev.event = v; else if (k === "data") ev.data.push(v);
  }
  return ev.data.length ? { ...ev, data: ev.data.join("\n") } : null;
}

function onServerEvent(type, d) {
  const refresh = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(loadPlan, 150); };
  if (type === "request.queued") { setBusy(true, d.position ? "Запрос в очереди…" : "Работаю над запросом…"); loadMessages().catch(() => {}); } // the message may come from the bot or another tab
  else if (type === "agent.progress") { S.progress = d.text; setBusy(true, S.progress); }
  else if (["plan.updated", "plan.ready", "clarification.required", "destinations.ready", "order.paid"].includes(type)) refresh();
  else if (type === "assistant.message") (async () => { S.progress = null; await loadMessages(); await loadPlan(); setBusy(false); })();
  else if (type === "request.failed") (async () => { S.progress = null; toast(d.reason || "Запрос не выполнен"); await loadMessages(); setBusy(false); })();
}

async function connect() {
  if (streamCtl) streamCtl.abort();
  const ctl = (streamCtl = new AbortController());
  // start after what the page already loaded over REST: the stream would otherwise replay the whole chat history
  let last = (await api(`/api/v1/chats/${S.chat}/status`)).last_event || 0, delay = 1000;
  (async () => {
    while (!ctl.signal.aborted) {
      // the server sends a keep-alive every 15 s: 35 s of silence means a dead connection that never reported an error
      const att = new AbortController(), onAbort = () => att.abort();
      ctl.signal.addEventListener("abort", onAbort);
      let seen = Date.now();
      const watchdog = setInterval(() => { if (Date.now() - seen > 35000) att.abort(); }, 5000);
      try {
        const res = await fetch(`/api/v1/chats/${S.chat}/events?after=${last}`, { headers: { Authorization: `Bearer ${S.token}`, Accept: "text/event-stream" }, signal: att.signal });
        if (res.status === 401 || res.status === 404) return showFatal("Сессия устарела. Обновите страницу.");
        if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
        showNet(false); delay = 1000;
        const reader = res.body.getReader(), dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          seen = Date.now();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          for (let i = buf.indexOf("\n\n"); i >= 0; i = buf.indexOf("\n\n")) {
            const ev = parseSse(buf.slice(0, i)); buf = buf.slice(i + 2);
            if (!ev) continue;
            if (ev.id) last = ev.id;
            try { onServerEvent(ev.event, JSON.parse(ev.data).payload || {}); } catch (e) { console.warn("bad event", e); }
          }
        }
      } catch (e) { if (ctl.signal.aborted) return; }
      finally { clearInterval(watchdog); ctl.signal.removeEventListener("abort", onAbort); }
      showNet(true);
      await sleep(delay); delay = Math.min(delay * 2, 15000);
    }
  })();
}

function showFatal(text) {
  $("fatal-text").textContent = text;
  $("fatal").classList.remove("hidden");
}

// ---------------------------------------------------------------- boot
function setupTabs() {
  const tabs = document.querySelectorAll("#tabs button");
  const show = (name) => {
    S.tab = name;
    for (const b of tabs) { const on = b.dataset.tab === name; b.classList.toggle("active", on); b.setAttribute("aria-selected", String(on)); }
    if (name === "plan") document.querySelector('#tabs [data-tab="plan"]').classList.remove("dot");
    for (const id of ["chat", "plan", "mapwrap"]) $(id).classList.toggle("active", id === name);
    if (name === "mapwrap") setTimeout(renderMap, 50);
  };
  for (const b of tabs) b.addEventListener("click", () => show(b.dataset.tab));
  show("chat");
}

async function boot() {
  if (tg) { tg.ready(); tg.expand(); }
  setupTabs();
  // the browser knows about a lost network before the open stream does
  window.addEventListener("offline", () => showNet(true));
  window.addEventListener("online", () => { if (S.chat) connect().catch(() => {}); });
  if (!navigator.onLine) showNet(true);
  $("modal-close").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  const input = $("input");
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); const t = input.value.trim(); if (!t) return; input.value = ""; input.style.height = "auto"; send(t); });
  initThemeToggle();
  $("new-chat").addEventListener("click", async () => { await ensureChat(true); S.messages = []; await Promise.all([loadMessages(), loadPlan()]); connect(); });

  if (!S.token) await authenticate();
  await ensureChat();
  initMap().catch((e) => { console.warn(e); $("map-note").textContent = "Карта не загрузилась"; $("map-note").classList.remove("hidden"); });
  await Promise.all([loadMessages(), loadPlan()]);
  await connect();
  // another tab (or the bot) may start a new trip: follow the user's active chat when this tab comes back or every 20 s
  document.addEventListener("visibilitychange", () => { if (!document.hidden) syncActiveChat(); });
  window.addEventListener("focus", syncActiveChat);
  setInterval(() => { if (!document.hidden) syncActiveChat(); }, 20000);
}

let syncing = false;
async function syncActiveChat() {
  if (syncing || !S.token) return;
  syncing = true;
  try {
    const c = await api("/api/v1/chats/active");
    if (c.id !== S.chat) {
      S.chat = c.id; localStorage.setItem("ekv_chat", S.chat);
      S.messages = []; S.progress = null; setBusy(false);
      await Promise.all([loadMessages(), loadPlan()]);
      connect();
    }
  } catch (e) { console.warn("active chat sync failed", e); } finally { syncing = false; }
}
$("fatal-retry").addEventListener("click", () => location.reload());
boot().catch((e) => { console.error(e); showFatal("Не удалось запустить приложение: " + e.message); });
