"""Sport identities from Winline's public navigation, without guessed requests."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Sport:
    key: str
    label: str
    slugs: tuple[str, ...]


SPORTS = (
    Sport("football", "Футбол", ("futbol",)),
    Sport("hockey", "Хоккей", ("xokkej", "hokkey", "hokkej")),
    Sport("tennis", "Теннис", ("tennis",)),
    Sport("basketball", "Баскетбол", ("basketbol",)),
    Sport("table_tennis", "Настольный теннис", ("nastolijnyj_tennis", "nastolnyj_tennis")),
    Sport("volleyball", "Волейбол", ("volejbol",)),
    Sport("mma", "ММА", ("mma",)),
    Sport("combat_sports", "Единоборства", ("edinoborstva",)),
    Sport("boxing", "Бокс", ("boks",)),
    Sport("handball", "Гандбол", ("gandbol",)),
    Sport("baseball", "Бейсбол", ("bejsbol",)),
    Sport("american_football", "Американский футбол", ("am_futbol",)),
    Sport("rugby", "Регби", ("regbi",)),
    Sport("futsal", "Футзал", ("futzal",)),
    Sport("beach_football", "Пляжный футбол", ("plyazhnyj_futbol",)),
    Sport("beach_volleyball", "Пляжный волейбол", ("plyazhnyj_volejbol",)),
    Sport("badminton", "Бадминтон", ("badminton",)),
    Sport("darts", "Дартс", ("darts",)),
    Sport("snooker", "Снукер", ("snuker",)),
    Sport("water_polo", "Водное поло", ("vodnoe_polo",)),
    Sport("floorball", "Флорбол", ("florbol",)),
    Sport("field_hockey", "Хоккей на траве", ("xokkej_na_trave", "hokkej_na_trave")),
    Sport("bandy", "Хоккей с мячом", ("xokkej_s_myachom", "hokkej_s_myachom")),
    Sport("cricket", "Крикет", ("kriket",)),
    Sport("motorsport", "Автоспорт", ("avtosport", "stock_car")),
    Sport("formula1", "Формула-1", ("formula_1", "formula-1", "formula1")),
    Sport("motorcycling", "Мотоспорт", ("motosport",)),
    Sport("golf", "Гольф", ("golijf", "golf")),
    Sport("curling", "Кёрлинг", ("kerling",)),
    Sport("pesapallo", "Песапалло", ("pesapallo",)),
)
BY_KEY = {sport.key: sport for sport in SPORTS}
BY_SLUG = {slug: sport.key for sport in SPORTS for slug in sport.slugs}
EXCLUDED = re.compile(r"kiber|cyber|esport|virtual|simulat|кибер|виртуал|симуляц|24\s*[xх/_-]?\s*7", re.I)
KEY = re.compile(r"[a-z][a-z0-9_\-]{0,49}")


def ordinary_sport(key: str) -> bool:
    return isinstance(key, str) and bool(KEY.fullmatch(key)) and not EXCLUDED.search(key)


def sport_label(key: str, observed_label: str | None = None) -> str:
    if key in BY_KEY:
        return BY_KEY[key].label
    if observed_label and not EXCLUDED.search(observed_label):
        return observed_label.strip()
    return key.replace("_", " ")


def sport_from_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in ("winline.ru", "www.winline.ru") or parsed.username or parsed.password:
        return None
    match = re.match(r"/(?:stavki|live)/sport/([a-zA-Z0-9_-]+)(?:/|$)", parsed.path)
    if not match:
        return None
    slug = match[1].lower()
    key = BY_SLUG.get(slug, slug)
    return key if ordinary_sport(key) else None
