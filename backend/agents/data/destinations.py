"""Curated destination catalog for the destination agent.

`hours_from_msk` and `visa` are static reference values (not live data): the UI labels them
as reference. Visa regimes change, so the UI shows them as "reference, check before booking". Climate comes from Open-Meteo at runtime; `sea` is an offshore point for the
marine API (None for non-sea destinations).
"""

# city id (see cities.py), tags, sea point, approx. flight hours from Moscow, visa regime for RU citizens
DESTINATIONS = [
    {"city": "sochi", "tags": ["море", "горы", "россия", "дети"], "sea": (43.5, 39.6), "hours_from_msk": 2.5, "visa": "без визы"},
    {"city": "krasnaya-polyana", "tags": ["горы", "лыжи", "россия"], "sea": None, "hours_from_msk": 2.7, "visa": "без визы"},
    {"city": "anapa", "tags": ["море", "россия", "дети"], "sea": (44.9, 37.2), "hours_from_msk": 2.3, "visa": "без визы"},
    {"city": "kgd", "tags": ["море", "город", "россия", "дети"], "sea": (54.95, 20.1), "hours_from_msk": 2.4, "visa": "без визы"},
    {"city": "spb", "tags": ["город", "культура", "россия"], "sea": None, "hours_from_msk": 1.5, "visa": "без визы"},
    {"city": "kzn", "tags": ["город", "гастро", "россия"], "sea": None, "hours_from_msk": 1.7, "visa": "без визы"},
    {"city": "mrv", "tags": ["горы", "санатории", "россия"], "sea": None, "hours_from_msk": 2.3, "visa": "без визы"},
    {"city": "irk", "tags": ["природа", "озеро", "россия"], "sea": None, "hours_from_msk": 6.0, "visa": "без визы"},
    {"city": "mmk", "tags": ["природа", "север", "россия"], "sea": None, "hours_from_msk": 2.0, "visa": "без визы"},
    {"city": "antalya", "tags": ["море", "дети", "всё включено"], "sea": (36.6, 30.7), "hours_from_msk": 3.7, "visa": "без визы"},
    {"city": "istanbul", "tags": ["город", "культура", "гастро"], "sea": None, "hours_from_msk": 3.3, "visa": "без визы"},
    {"city": "hurghada", "tags": ["море", "дети", "всё включено", "дайвинг"], "sea": (27.2, 33.9), "hours_from_msk": 4.3, "visa": "по прилёту"},
    {"city": "sharm", "tags": ["море", "дайвинг", "всё включено"], "sea": (27.8, 34.4), "hours_from_msk": 4.5, "visa": "по прилёту"},
    {"city": "dubai", "tags": ["море", "город", "шопинг", "дети"], "sea": (25.3, 55.1), "hours_from_msk": 5.0, "visa": "без визы"},
    {"city": "phuket", "tags": ["море", "дети", "тропики"], "sea": (7.8, 98.2), "hours_from_msk": 9.5, "visa": "без визы"},
    {"city": "nhatrang", "tags": ["море", "тропики", "дети"], "sea": (12.2, 109.3), "hours_from_msk": 10.0, "visa": "без визы"},
    {"city": "colombo", "tags": ["море", "тропики", "природа"], "sea": (6.9, 79.7), "hours_from_msk": 8.0, "visa": "электронная"},
    {"city": "male", "tags": ["море", "тропики", "пляж"], "sea": (4.2, 73.4), "hours_from_msk": 8.0, "visa": "по прилёту"},
    {"city": "denpasar", "tags": ["море", "тропики", "природа"], "sea": (-8.8, 115.2), "hours_from_msk": 12.0, "visa": "по прилёту"},
    {"city": "sanya", "tags": ["море", "тропики"], "sea": (18.2, 109.6), "hours_from_msk": 10.5, "visa": "без визы"},
    {"city": "batumi", "tags": ["море", "город", "горы"], "sea": (41.6, 41.5), "hours_from_msk": 3.0, "visa": "без визы"},
    {"city": "tbilisi", "tags": ["город", "гастро", "горы"], "sea": None, "hours_from_msk": 2.8, "visa": "без визы"},
    {"city": "yerevan", "tags": ["город", "гастро", "культура"], "sea": None, "hours_from_msk": 3.0, "visa": "без визы"},
    {"city": "baku", "tags": ["город", "культура"], "sea": None, "hours_from_msk": 3.2, "visa": "без визы"},
    {"city": "samarkand", "tags": ["город", "культура"], "sea": None, "hours_from_msk": 4.2, "visa": "без визы"},
    {"city": "almaty", "tags": ["горы", "город", "лыжи"], "sea": None, "hours_from_msk": 4.7, "visa": "без визы"},
    {"city": "tokyo", "tags": ["город", "культура", "гастро"], "sea": None, "hours_from_msk": 9.5, "visa": "нужна"},
    {"city": "osaka", "tags": ["город", "гастро", "культура"], "sea": None, "hours_from_msk": 10.0, "visa": "нужна"},
    {"city": "seoul", "tags": ["город", "культура"], "sea": None, "hours_from_msk": 9.0, "visa": "без визы"},
]
