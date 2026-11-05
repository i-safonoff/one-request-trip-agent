"""The main agent's answer (`AgentTurn`) and the facts it is allowed to speak from.

Code builds the facts: understood fields, recommendations (why / trade-offs / risks), changes since
the previous revision, checks. The LLM only phrases them. Before an answer is sent, every number in
the text must exist in the facts; otherwise the deterministic text below is used instead.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel

from . import fmt
from .budget import compute_pricing
from .models import Check, TripState
from . import hotel_fit
from .scoring import tradeoffs, why_selected


class Understood(BaseModel):
    field: str
    value: str
    origin: str = "user"  # user | agent | derived


class Recommendation(BaseModel):
    node_id: str
    offer_id: str
    title: str
    why: list[str] = []
    tradeoffs: list[dict[str, Any]] = []
    risks: list[str] = []
    data_mode: str = "live"


class Change(BaseModel):
    node_id: str
    label: str = ""
    before: str | None
    after: str | None
    delta: str | None = None


class AgentTurn(BaseModel):
    plan_revision: int
    kind: Literal["plan", "question", "destinations", "change", "checkout", "info", "error"] = "plan"
    text: str
    understood: list[Understood] = []
    recommendations: list[Recommendation] = []
    changes: list[Change] = []
    questions: list[dict[str, Any]] = []
    quick_replies: list[str] = []


RISK_TEXT = {
    "NODE_NOT_FOUND": lambda f: "по этому участку нет подходящих вариантов под ваши условия",
    "NODE_UNSEARCHED": lambda f: ("варианты по этому участку устарели после изменения плана, а новый поиск не удался: цена и состав не подтверждены"
                                  if f.get("status") == "stale" else "по этому участку поиск не завершён"),
    "TRIP_DATES_MISMATCH": lambda f: (f"проживания идут {fmt.day_iso(f['stays_from'])} — {fmt.day_iso(f['stays_to'])}, "
                                      f"а поездка запрошена на {fmt.day_iso(f['requested_from'])} — {fmt.day_iso(f['requested_to'])}"),
    "DATES_STAY_ARRIVAL": lambda f: f"прибытие {fmt.day_iso(f['arrival_date'])} не совпадает с заселением {fmt.day_iso(f['check_in'])}",
    "DATES_STAY_DEPARTURE": lambda f: f"отправление {fmt.day_iso(f['departure_date'])} не совпадает с выездом из отеля {fmt.day_iso(f['check_out'])}",
    "NIGHTS_COVERAGE": lambda f: "проживания в соседних городах не стыкуются по ночам",
    "PLACE_CHAIN": lambda f: f"билет приходит в {f.get('arrives')}, это далеко от места проживания",
    "MISSING_TRANSFER": lambda f: f"от {f.get('arrives')} до отеля около {f.get('distance_km')} км, а трансфера в плане нет",
    "EARLY_ARRIVAL": lambda f: f"прибытие примерно за {fmt.num(f['wait_hours'])} ч до заселения ({f['check_in_time']}): нужен ранний заезд или камера хранения",
    "NIGHT_ARRIVAL": lambda f: "ночное прибытие: предупредите отель о заезде" if not f.get("front_desk_24h") else "ночное прибытие, стойка отеля круглосуточная",
    "LATE_CHECKIN": lambda f: "поздний заезд: уточните у отеля, что вас дождутся",
    "LATE_DEPARTURE_GAP": lambda f: f"между выездом из отеля ({f['check_out_time']}) и отъездом около {fmt.num(f['hours_between'])} ч: можно попросить поздний выезд",
    "DEPARTURE_BUFFER": lambda f: "очень ранний выезд из отеля: закажите трансфер заранее",
    "CONNECTION_TIME": lambda f: f"пересадка в {f['at']} всего {f['gap_min']} мин при нужных {f['need_min']}",
    "TZ_DATE_SHIFT": lambda f: "из-за часовых поясов или ночного рейса прибытие приходится на другую дату",
    "PARTY_CAPACITY": lambda f: "для расчёта нужен возраст ребёнка" if f.get("children_unknown") else "номер не рассчитан на такую компанию",
    "BUDGET": lambda f: (f"сумма {fmt.rub(f['total'])} выше бюджета {fmt.rub(f['budget'])} на {fmt.rub(f['over'])}" if "over" in f
                         else "бюджет проверен только по найденным вариантам: расчёт неполный"),
    "CANCELLATION_EXPIRED": lambda f: "срок бесплатной отмены уже прошёл",
    "OFFER_STALE": lambda f: "цена получена давно, перед оплатой перепроверим",
    "CONSTRAINT_UNKNOWN": lambda f: f"источник не подтверждает условие «{COND_LABEL.get(f['kind'], f['kind'])}»: проверьте вручную",
    "CONSTRAINT_FAIL": lambda f: f"не выполнено условие «{COND_LABEL.get(f['kind'], f['kind'])}»",
}
COND_LABEL = {"bed_type": "тип кровати", "view": "вид из номера", "breakfast": "завтрак", "free_cancellation": "бесплатная отмена", "pets_allowed": "можно с животными",
              "kid_friendly": "подходит для детей", "min_stars": "звёздность", "min_rating": "рейтинг",
              "max_travel_hours": "время в пути", "max_transfers": "пересадки", "avoid_overnight": "без ночного переезда",
              "only_modes": "выбранный вид транспорта", "exclude_modes": "исключённый вид транспорта"}
_MODE_NOUN = {"flight": ("самолёт", "самолётов"), "rail": ("поезд", "поездов"), "bus": ("автобус", "автобусов")}


def cond_label(c) -> str:
    """The user's words for a condition: never the internal kind ("only_modes", "free_cancellation")."""
    if c.kind in ("only_modes", "exclude_modes"):
        vals = c.value if isinstance(c.value, list) else [c.value]
        names = [_MODE_NOUN.get(str(v), (str(v), str(v))) for v in vals]
        return "только " + ", ".join(n[0] for n in names) if c.kind == "only_modes" else "без " + ", ".join(n[1] for n in names)
    return COND_LABEL.get(c.kind) or c.label or c.kind


def risk_text(c: Check) -> str:
    fn = RISK_TEXT.get(c.code)
    try:
        return fn(c.facts) if fn else c.code
    except (KeyError, TypeError, ValueError):
        return c.code


def node_label(state: TripState, n) -> str:
    name = lambda pid: (state.places[pid].name if pid in state.places else pid) or pid  # noqa: E731
    if n.type == "stay":
        return f"Проживание: {name(n.place)}"
    return {"transport": "Переезд", "transfer": "Трансфер"}[n.type] + f": {name(n.from_)} → {name(n.to)}"


def understood(state: TripState) -> list[Understood]:
    out: list[Understood] = []
    names = lambda ids: ", ".join(state.places[i].name or i for i in ids if i in state.places)  # noqa: E731
    if state.route:
        out.append(Understood(field="destination", value=names([s.place for s in state.route]),
                              origin="user" if "route" in state.facts else "agent"))
    if state.origin:
        out.append(Understood(field="origin", value=names([state.origin])))
    if state.date_from:
        out.append(Understood(field="dates", value=fmt.day_range(state.date_from, state.date_to)))
    p = state.party
    if p.adults:
        kids = f", детей: {len(p.children_ages) + p.children_unknown}" if (p.children_ages or p.children_unknown) else ""
        out.append(Understood(field="party", value=f"{fmt.plural(p.adults, 'взрослый', 'взрослых', 'взрослых')}{kids}"))
    b = state.budget
    if b.amount is not None:
        scope = {"trip": "за всю поездку", "stay": "на проживание", "transport": "на переезды"}[b.scope]
        out.append(Understood(field="budget", value=f"до {fmt.rub(b.amount, b.currency)} {scope}",
                              origin=(b.source.origin if b.source else "user")))
    elif b.qualitative:
        out.append(Understood(field="budget", value=b.qualitative))
    for c in state.constraints:
        out.append(Understood(field=f"constraint:{c.kind}", value=(cond_label(c)
                              + ("" if c.strength == "hard" else " (по возможности)"))))
    return out


def recommendations(state: TripState) -> list[Recommendation]:
    recs: list[Recommendation] = []
    by_node: dict[str, list[str]] = {}
    for c in state.checks:
        if c.severity in ("error", "warning"):
            for nid in c.node_ids:
                by_node.setdefault(nid, []).append(risk_text(c))
    for nid in state.sequence:
        n = state.nodes[nid]
        if n.type == "transfer" or not n.selected_offer_ref or n.selected_offer_ref not in state.offers:
            continue
        o = state.offers[n.selected_offer_ref]
        risks = list(dict.fromkeys(by_node.get(nid, [])))
        if n.type == "stay":
            risks += hotel_fit.disappointment(state, o).reasons[:3] + hotel_fit.feedback_risks(o)
        if o.data_mode == "mock":
            risks.append("демо-данные: цена и расписание сгенерированы")
        if n.coverage == "partial":
            risks.append("просмотрена часть выдачи источника: это лучший из найденных, не гарантированно лучший на рынке")
        recs.append(Recommendation(node_id=nid, offer_id=o.id, title=o.title(), why=why_selected(state, n),
                                   tradeoffs=tradeoffs(state, n), risks=risks, data_mode=o.data_mode))
    return recs


def changes_between(before: TripState | None, after: TripState) -> list[Change]:
    if before is None or not any(n.selected_offer_ref for n in before.nodes.values()):
        return []  # nothing was proposed before: the first plan is not a "change"
    out: list[Change] = []

    def label(s: TripState, nid: str) -> tuple[str | None, Any]:
        n = s.nodes.get(nid)
        o = s.offers.get(n.selected_offer_ref) if n and n.selected_offer_ref else None
        return (None, None) if not o else (o.title() + f" — {fmt.rub(o.price.amount, o.price.currency)}", o.price.amount)
    for nid in after.sequence:
        if after.nodes[nid].type == "transfer":
            continue
        b, bp = label(before, nid)
        a, ap = label(after, nid)
        if b != a:
            out.append(Change(node_id=nid, label=node_label(after, after.nodes[nid]), before=b, after=a,
                              delta=(str(ap - bp) if (ap is not None and bp is not None) else None)))
    for nid in before.sequence:
        if nid not in after.nodes and before.nodes[nid].type != "transfer":
            out.append(Change(node_id=nid, label=node_label(before, before.nodes[nid]), before=label(before, nid)[0], after=None))
    return out


def build_facts(state: TripState, before: TripState | None) -> dict[str, Any]:
    pricing = compute_pricing(state)
    return {
        "revision": state.revision,
        "understood": [u.model_dump() for u in understood(state)],
        "dates": fmt.day_range(state.date_from, state.date_to),
        "pricing": pricing,
        "budget": state.budget.model_dump(mode="json"),
        "recommendations": [r.model_dump() for r in recommendations(state)],
        "changes": [c.model_dump() for c in changes_between(before, state)],
        "checks": [{"code": c.code, "severity": c.severity, "text": risk_text(c)} for c in state.checks],
        "notes": [d.text for d in state.data_notes],
        "state_notes": {"unmet_soft": [c.label or c.kind for c in state.constraints if c.kind == "free_text"]},
    }


# ---------------------------------------------------------------- deterministic text

def deterministic_text(state: TripState, facts: dict[str, Any], kind: str) -> str:
    lines: list[str] = []
    und = [u for u in facts["understood"] if not u["field"].startswith("constraint:")]
    cons = [u["value"] for u in facts["understood"] if u["field"].startswith("constraint:")]
    if und:
        lines.append("Понял так: " + "; ".join(f"{u['value']}" for u in und) + (f". Условия: {', '.join(cons)}." if cons else "."))
    recs = facts["recommendations"]
    pr = facts["pricing"]
    if recs:
        head = "Собрал поездку." if kind != "change" else "Обновил план."
        if pr["is_complete"] and pr["total"]:
            head += f" Итого {fmt.rub(pr['total'], pr['currency'])}"
            head += " (без трансферов)." if pr["excluded"] else "."
        else:
            head += f" Пока известно {fmt.rub(pr['known_subtotal'], pr['currency'])}, расчёт неполный."
        lines.append(head)
        for r in recs:
            lines.append(f"• {r['title']}")
            for w in r["why"][:3]:
                lines.append(f"  — {w}")
            for t in r["tradeoffs"][:1]:
                lines.append(f"  По сравнению с «{t['vs_title']}»: {t['text']}.")
            for k in r["risks"][:2]:
                lines.append(f"  ⚠ {k}")
    elif kind == "plan":
        if any(d.kind == "unverified" and d.scope.startswith("search:") for d in state.data_notes):
            lines.append("Поиск не завершился: источник данных не ответил, вариантов пока нет.")  # not "nothing fits"
        else:
            lines.append("Подходящих вариантов под эти условия не нашлось.")
    if facts["changes"]:
        lines.append("Что изменилось: " + "; ".join(
            f"{c['label'] or c['node_id']}: {c['before'] or '—'} → {c['after'] or '—'}" for c in facts["changes"][:4]))
    for note in facts["notes"][:2]:
        lines.append(f"Важно: {note}")
    errs = [c["text"] for c in facts["checks"] if c["severity"] == "error" and c["code"] in ("BUDGET", "NODE_NOT_FOUND")]
    for e in dict.fromkeys(errs):
        lines.append(f"Не сходится: {e}.")
    return "\n".join(lines) or "План обновлён."


# ---------------------------------------------------------------- number check

_NUM = re.compile(r"\d[\d\u00a0\u202f ]*(?:[.,]\d+)?")
_SEP = re.compile(r"[\u00a0\u202f ]")


def _norm(tok: str) -> str:
    tok = tok.replace(",", ".")
    return tok.rstrip("0").rstrip(".") if "." in tok else tok


def numbers_in(text: str) -> set[str]:
    """Every way a number can be read in `text` (with and without thousand separators): the facts side."""
    out: set[str] = set()
    for m in re.finditer(r"\d{4}-\d{2}-\d{2}", text):  # ISO dates -> year, month, day
        out.update(str(int(x)) for x in m.group(0).split("-"))
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    for m in _NUM.finditer(text):
        raw = m.group(0).strip()
        out.add(_norm(_SEP.sub("", raw)))
        out.update(_norm(p) for p in raw.replace("\u00a0", " ").replace("\u202f", " ").split())
    return out


def _claims(text: str) -> list[tuple[str, list[str]]]:
    """Numbers a text asserts: (compact reading, separate readings). Supported if either reading is in the facts."""
    out = []
    for m in _NUM.finditer(text):
        raw = m.group(0).strip()
        parts = [_norm(p) for p in raw.replace("\u00a0", " ").replace("\u202f", " ").split()]
        out.append((_norm(_SEP.sub("", raw)), parts))
    return out


def unsupported_numbers(text: str, facts: dict[str, Any], extra: str = "") -> set[str]:
    allowed = numbers_in(json.dumps(facts, ensure_ascii=False, default=str) + " " + extra)
    return {compact for compact, parts in _claims(text) if compact not in allowed and not all(p in allowed for p in parts)}


_QUOTED = re.compile(r"[«\"“]([^»\"”]{3,80})[»\"”]")
_PROMISE = re.compile(r"гарантир\w*|гарантия|100\s?%|обязательно\s+(?:будет|получите)|без\s+риска", re.I)


_DIRECT = re.compile(r"прям\w*\s+(?:рейс|перел|поезд|авиарейс)|без\s+пересад", re.I)
_ALL_FINE = re.compile(r"всё\s+соответству|без\s+(?:известных\s+)?риск|нет\s+(?:известных\s+)?риск|нарушений\s+нет|проблем\s+нет", re.I)


def contradicted_claims(text: str, state) -> list[str]:
    """Statements the plan itself contradicts: "direct flight" over a selected offer with transfers, "everything fits"
    next to a failed check."""
    out = []
    if _DIRECT.search(text) and any(o.transport and o.transport.transfers > 0
                                    for n in state.nodes.values() if n.type == "transport"
                                    for o in [state.offers.get(n.selected_offer_ref or "")] if o):
        out.append("claim «прямой рейс» при выбранных пересадках")
    if _ALL_FINE.search(text) and any(c.severity == "error" for c in state.checks):
        out.append("claim «всё в порядке» при ошибке состыковки")
    return out


def unsupported_claims(text: str, facts_text: str) -> list[str]:
    """What an answer asserts that the facts do not contain: a quoted name (hotel, carrier, city) that is in none of
    the facts, or a guarantee the data cannot give. Numbers are checked separately (`unsupported_numbers`)."""
    low = facts_text.lower().replace("ё", "е")
    out = [f"name «{n}»" for n in _QUOTED.findall(text) if n.lower().replace("ё", "е") not in low]
    out += [f"promise «{m.group(0)}»" for m in _PROMISE.finditer(text) if m.group(0).lower() not in low]
    return out
