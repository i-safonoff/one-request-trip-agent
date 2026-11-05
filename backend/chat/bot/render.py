"""Telegram texts and keyboards. Everything dynamic is HTML-escaped: plan and agent text is untrusted."""
import html
import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from shared.config import Settings

START_TEXT = (
    "Привет! Я помогу спланировать поездку: подберу отели и переезды под ваши даты и бюджет.\n\n"
    "Просто опишите, куда и когда хотите поехать, например:\n"
    "<i>Москва, 6–9 ноября, двое взрослых, завтрак и бесплатная отмена, до 30 000 ₽</i>\n\n"
    "Потом можно уточнять: «сделай дешевле», «добавь ещё двух взрослых», «перенеси на два дня позже».\n\n"
    "/plan: открыть карту поездки\n"
    "/status: что сейчас происходит\n"
    "/new: начать новую поездку\n"
    "/help: подсказка"
)
HELP_TEXT = (
    "Опишите поездку обычным сообщением, я соберу план и пришлю ссылку на карту.\n\n"
    "Команды:\n"
    "/plan: показать текущий план и открыть карту\n"
    "/status: статус запроса и открытые вопросы\n"
    "/new: начать новую поездку (текущая сохранится)\n"
    "/history: прошлые поездки, оценить или повторить\n"
    "/profile: что я о вас запомнил\n"
    "/help: эта подсказка\n\n"
    "Если я задал вопрос, ответьте на него сообщением или нажмите кнопку."
)
START_TEXT_APP = (
    "Привет! Я помогу спланировать поездку: подберу отели и переезды, сравню варианты и доведу до оформления.\n\n"
    "Всё происходит в приложении: чат с агентом, план, карта и оформление в одном месте, поэтому данные везде одинаковые. "
    "Нажмите кнопку «🗺 Открыть приложение» или «🗺 Карта» слева от поля ввода.\n\n"
    "/history: прошлые поездки, оценить или повторить\n"
    "/profile: что я о вас запомнил\n"
    "/help: подсказка"
)
HELP_TEXT_APP = (
    "Планирование и оформление работают только в приложении. Откройте его кнопкой ниже или «🗺 Карта» рядом с полем ввода: "
    "там можно написать запрос, уточнить условия, пересобрать план и оформить заказ.\n\n"
    "В чате доступны:\n"
    "/new: начать новую поездку (откроется в приложении)\n"
    "/history: прошлые поездки, оценить или повторить\n"
    "/profile: что я о вас запомнил\n"
    "/help: эта подсказка"
)
APP_REDIRECT = "Планирование, пересборка плана и оформление работают в приложении. Откройте его кнопкой ниже, и ваш запрос будет там."
DEMO_OFF = "Готовых демо-планов в этой версии нет: опишите поездку словами, например «Москва, 6–9 ноября, двое взрослых»."
DEMO_USAGE = "Так: /demo (Москва и Санкт-Петербург) или /demo japan (Токио и Осака)."
ONLY_TEXT = "Пока я понимаю только текстовые сообщения. Опишите поездку словами."
UNKNOWN_COMMAND = "Не знаю такой команды. Список команд: /help"
NEW_TRIP = "Начинаем новую поездку. Предыдущая сохранена. Куда и когда хотите поехать?"
NO_PLAN = "Плана пока нет. Опишите поездку, и я его соберу."
QUESTION_CLOSED = "Этот вопрос уже закрыт."

esc = html.escape


def map_url(settings: Settings, plan_id: uuid.UUID | str) -> str:
    # the Mini App is the whole web UI (chat + plan + map + cart), not only the map; plan_id lets it open that version
    return f"{settings.public_base_url.rstrip('/')}/?plan_id={plan_id}"


def miniapp_only(settings: Settings) -> bool:
    mode = (settings.telegram_chat_mode or "auto").lower()
    return mode == "miniapp" or (mode == "auto" and _is_https(settings))


def app_keyboard(settings: Settings) -> InlineKeyboardMarkup | None:
    """A button that opens the whole app (chat, plan, map, checkout) inside Telegram."""
    if not _is_https(settings):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗺 Открыть приложение", web_app=WebAppInfo(url=settings.public_base_url.rstrip("/") + "/"))]])


def _is_https(settings: Settings) -> bool:
    return settings.public_base_url.lower().startswith("https://")


def map_keyboard(settings: Settings, plan_id: uuid.UUID | str | None) -> InlineKeyboardMarkup | None:
    """Button that opens the Mini App. Telegram only accepts https for web apps, so dev setups get a text link."""
    if plan_id is None or not _is_https(settings):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗺 Открыть карту", web_app=WebAppInfo(url=map_url(settings, plan_id)))]])


def map_link_text(settings: Settings, plan_id: uuid.UUID | str | None) -> str:
    """Fallback when there is no web-app button (http dev URL)."""
    if plan_id is None or _is_https(settings):
        return ""
    return f"\n\nКарта: {esc(map_url(settings, plan_id))}"


def clarification_keyboard(clarification_id: uuid.UUID | str, options: list[str] | None) -> InlineKeyboardMarkup | None:
    if not options:
        return None
    buttons = [InlineKeyboardButton(text=opt[:40], callback_data=f"clar:{clarification_id}:{i}")
               for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def suggestions_keyboard(items) -> InlineKeyboardMarkup:
    """One row per suggestion: accept / dismiss. The `sug:` callbacks are handled by the history module."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ {i.text}"[:48], callback_data=f"sug:{i.suggestion_id}:y"),
        InlineKeyboardButton(text="✖", callback_data=f"sug:{i.suggestion_id}:n")] for i in items])


def chunks(text: str, size: int = 4000) -> list[str]:
    """Split on paragraph/line/word boundaries so a long agent answer fits Telegram's 4096-char limit."""
    parts = []
    while len(text) > size:
        cut = max(text.rfind("\n\n", 0, size), text.rfind("\n", 0, size), text.rfind(" ", 0, size))
        cut = cut if cut > size // 2 else size
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + [text] if text else parts
