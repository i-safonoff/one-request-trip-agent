"""Reference cities: coordinates and IANA time zones. Used to resolve names without network
access and to estimate the time zone of any other place (nearest catalog city)."""
import re

# id, name, aliases, country, lat, lon, tz
_ROWS = [
    ("msk", "Москва", ["moscow", "мск"], "Россия", 55.7558, 37.6173, "Europe/Moscow"),
    ("spb", "Санкт-Петербург", ["питер", "петербург", "спб", "saint petersburg", "st petersburg"], "Россия", 59.9343, 30.3351, "Europe/Moscow"),
    ("kzn", "Казань", ["kazan"], "Россия", 55.7961, 49.1064, "Europe/Moscow"),
    ("nnov", "Нижний Новгород", ["нижний", "nizhny novgorod"], "Россия", 56.2965, 43.9361, "Europe/Moscow"),
    ("sochi", "Сочи", ["sochi"], "Россия", 43.5855, 39.7231, "Europe/Moscow"),
    ("krasnaya-polyana", "Красная Поляна", ["роза хутор", "эсто-садок"], "Россия", 43.6836, 40.2053, "Europe/Moscow"),
    ("kgd", "Калининград", ["kaliningrad"], "Россия", 54.7104, 20.4522, "Europe/Kaliningrad"),
    ("ekb", "Екатеринбург", ["ekaterinburg"], "Россия", 56.8389, 60.6057, "Asia/Yekaterinburg"),
    ("nsk", "Новосибирск", ["novosibirsk"], "Россия", 55.0084, 82.9357, "Asia/Novosibirsk"),
    ("krd", "Краснодар", ["krasnodar"], "Россия", 45.0355, 38.9753, "Europe/Moscow"),
    ("mrv", "Минеральные Воды", ["кмв", "кисловодск", "пятигорск"], "Россия", 44.2088, 43.1386, "Europe/Moscow"),
    ("mmk", "Мурманск", ["murmansk"], "Россия", 68.9585, 33.0827, "Europe/Moscow"),
    ("vvo", "Владивосток", ["vladivostok"], "Россия", 43.1155, 131.8855, "Asia/Vladivostok"),
    ("irk", "Иркутск", ["байкал", "listvyanka", "листвянка"], "Россия", 52.2869, 104.3050, "Asia/Irkutsk"),
    ("kuf", "Самара", ["samara"], "Россия", 53.1959, 50.1002, "Europe/Samara"),
    ("ufa", "Уфа", ["ufa"], "Россия", 54.7388, 55.9721, "Asia/Yekaterinburg"),
    ("yar", "Ярославль", ["yaroslavl"], "Россия", 57.6261, 39.8845, "Europe/Moscow"),
    ("suzdal", "Суздаль", ["suzdal"], "Россия", 56.4194, 40.4489, "Europe/Moscow"),
    ("vnov", "Великий Новгород", ["veliky novgorod", "новгород"], "Россия", 58.5228, 31.2699, "Europe/Moscow"),
    ("pkv", "Псков", ["pskov"], "Россия", 57.8136, 28.3496, "Europe/Moscow"),
    ("perm", "Пермь", ["perm"], "Россия", 58.0105, 56.2502, "Asia/Yekaterinburg"),
    ("rostov", "Ростов-на-Дону", ["ростов", "rostov-on-don"], "Россия", 47.2357, 39.7015, "Europe/Moscow"),
    ("vlg", "Волгоград", ["volgograd"], "Россия", 48.7080, 44.5133, "Europe/Volgograd"),
    ("anapa", "Анапа", ["anapa"], "Россия", 44.8948, 37.3167, "Europe/Moscow"),
    ("gelendzhik", "Геленджик", ["gelendzhik"], "Россия", 44.5611, 38.0768, "Europe/Moscow"),
    ("gorno-altaysk", "Горно-Алтайск", ["алтай", "altai"], "Россия", 51.9581, 85.9603, "Asia/Barnaul"),
    ("mhk", "Махачкала", ["дагестан", "dagestan"], "Россия", 42.9849, 47.5047, "Europe/Moscow"),
    ("petrozavodsk", "Петрозаводск", ["карелия", "karelia"], "Россия", 61.7849, 34.3469, "Europe/Moscow"),
    ("tula", "Тула", ["tula"], "Россия", 54.1961, 37.6182, "Europe/Moscow"),
    ("tokyo", "Токио", ["tokyo"], "Япония", 35.6762, 139.6503, "Asia/Tokyo"),
    ("osaka", "Осака", ["osaka"], "Япония", 34.6937, 135.5023, "Asia/Tokyo"),
    ("kyoto", "Киото", ["kyoto"], "Япония", 35.0116, 135.7681, "Asia/Tokyo"),
    ("antalya", "Анталья", ["antalya", "анталия", "кемер", "белек", "аланья"], "Турция", 36.8969, 30.7133, "Europe/Istanbul"),
    ("istanbul", "Стамбул", ["istanbul"], "Турция", 41.0082, 28.9784, "Europe/Istanbul"),
    ("hurghada", "Хургада", ["hurghada"], "Египет", 27.2579, 33.8116, "Africa/Cairo"),
    ("sharm", "Шарм-эль-Шейх", ["шарм", "sharm el sheikh"], "Египет", 27.9158, 34.3300, "Africa/Cairo"),
    ("dubai", "Дубай", ["dubai", "оаэ", "uae"], "ОАЭ", 25.2048, 55.2708, "Asia/Dubai"),
    ("abudhabi", "Абу-Даби", ["abu dhabi"], "ОАЭ", 24.4539, 54.3773, "Asia/Dubai"),
    ("phuket", "Пхукет", ["phuket"], "Таиланд", 7.8804, 98.3923, "Asia/Bangkok"),
    ("bangkok", "Бангкок", ["bangkok", "таиланд", "тайланд"], "Таиланд", 13.7563, 100.5018, "Asia/Bangkok"),
    ("nhatrang", "Нячанг", ["nha trang", "вьетнам"], "Вьетнам", 12.2388, 109.1967, "Asia/Ho_Chi_Minh"),
    ("colombo", "Коломбо", ["шри-ланка", "шри ланка", "sri lanka"], "Шри-Ланка", 6.9271, 79.8612, "Asia/Colombo"),
    ("male", "Мале", ["мальдивы", "maldives"], "Мальдивы", 4.1755, 73.5093, "Indian/Maldives"),
    ("denpasar", "Бали", ["denpasar", "bali", "денпасар"], "Индонезия", -8.6705, 115.2126, "Asia/Makassar"),
    ("tbilisi", "Тбилиси", ["tbilisi"], "Грузия", 41.7151, 44.8271, "Asia/Tbilisi"),
    ("batumi", "Батуми", ["batumi"], "Грузия", 41.6168, 41.6367, "Asia/Tbilisi"),
    ("yerevan", "Ереван", ["yerevan"], "Армения", 40.1792, 44.4991, "Asia/Yerevan"),
    ("baku", "Баку", ["baku"], "Азербайджан", 40.4093, 49.8671, "Asia/Baku"),
    ("samarkand", "Самарканд", ["samarkand"], "Узбекистан", 39.6270, 66.9750, "Asia/Samarkand"),
    ("almaty", "Алматы", ["almaty", "алма-ата"], "Казахстан", 43.2220, 76.8512, "Asia/Almaty"),
    ("minsk", "Минск", ["minsk"], "Беларусь", 53.9006, 27.5590, "Europe/Minsk"),
    ("seoul", "Сеул", ["seoul"], "Южная Корея", 37.5665, 126.9780, "Asia/Seoul"),
    ("beijing", "Пекин", ["beijing"], "Китай", 39.9042, 116.4074, "Asia/Shanghai"),
    ("sanya", "Санья", ["sanya", "хайнань", "hainan"], "Китай", 18.2528, 109.5120, "Asia/Shanghai"),
    ("paris", "Париж", ["paris"], "Франция", 48.8566, 2.3522, "Europe/Paris"),
    ("rome", "Рим", ["rome"], "Италия", 41.9028, 12.4964, "Europe/Rome"),
    ("barcelona", "Барселона", ["barcelona"], "Испания", 41.3874, 2.1686, "Europe/Madrid"),
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", s.lower().replace("ё", "е")).strip()


CITIES = {r[0]: {"id": r[0], "name": r[1], "aliases": r[2], "country": r[3],
                 "lat": r[4], "lon": r[5], "tz": r[6]} for r in _ROWS}
_INDEX: dict[str, str] = {}
for _c in CITIES.values():
    _INDEX[_norm(_c["name"])] = _c["id"]
    for _a in _c["aliases"]:
        _INDEX.setdefault(_norm(_a), _c["id"])


def find_city(name: str | None) -> dict | None:
    """Catalog lookup by name or alias ('Питер', 'Petersburg' -> spb)."""
    if not name:
        return None
    key = _norm(name)
    if key in _INDEX:
        return CITIES[_INDEX[key]]
    for k, cid in _INDEX.items():  # 'в санкт петербурге' style inflections: prefix match on 5+ chars
        if len(k) >= 5 and (key.startswith(k[:-1]) or k.startswith(key[:-1])) and len(key) >= 5:
            return CITIES[cid]
    return None
