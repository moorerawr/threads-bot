"""
Threads-бот: генерує пости через Gemini в мікс-стилі та публікує 4 рази на добу.

Запуск:
  python threads_bot.py              -> працює постійно, 4 пости на день (09:00-24:00 за Києвом)
  python threads_bot.py --once       -> згенерувати й опублікувати один пост просто зараз
  python threads_bot.py --preview 5  -> показати 5 тестових постів БЕЗ публікації
"""

import argparse
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from google import genai
from google.genai import errors, types

import config

# ───────────────────────── НАЛАШТУВАННЯ ─────────────────────────

PREFERRED_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
]
FIRST_MODEL_RETRIES = 5   # головну модель мучимо довше (вона найкраща)
RETRIES_PER_MODEL = 3     # решту моделей пробуємо менше
THREADS_LIMIT = 500

BAIT_CHANCE = 0.30          # ймовірність «байту» на коментарі
PROVOKE_CHANCE = 0.20       # ймовірність легкої провокації
SECOND_STYLE_CHANCE = 0.15  # ймовірність підмішати нотку другого стилю

# Вікна публікації (хвилини від 00:00 за Києвом). Час у вікні обирається випадково.
SLOT_WINDOWS = [
    (9 * 60, 11 * 60 + 30),   # ранок:   09:00-11:30
    (12 * 60, 15 * 60),       # день:    12:00-15:00
    (16 * 60, 19 * 60),       # вечір:   16:00-19:00
    (20 * 60, 23 * 60 + 30),  # ніч:     20:00-23:30
]

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"      # що вже опубліковано сьогодні
HISTORY_FILE = BASE_DIR / "history.json"  # останні пости, щоб не повторюватись
HISTORY_SIZE = 30

# ───────────────────────── ЧАС (КИЇВ) ─────────────────────────


def _load_tz():
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None
    for name in ("Europe/Kyiv", "Europe/Kiev"):
        try:
            return ZoneInfo(name)
        except Exception:
            continue
    print("⚠️ Часовий пояс Києва не знайдено. Виконай: pip install tzdata")
    print("   Поки що використовується час твого комп'ютера.")
    return None


KYIV_TZ = _load_tz()


def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ) if KYIV_TZ else datetime.now()


# ───────────────────────── БАНК СТИЛІВ ─────────────────────────
# Головне: прості, життєві пости на ОДНУ думку. Приклади показують інтонацію.

LEN_TINY = "дуже коротко: 1-2 речення, до 150 символів"
LEN_SHORT = "коротко: 2-4 речення, до 300 символів"
LEN_MEDIUM = "середньо: до 400 символів, короткі абзаци по 1-2 речення"

STYLES = [
    {
        "name": "Питання до аудиторії",
        "weight": 20,
        "bait": False,  # саме питання вже є прийомом на коментарі
        "lengths": [LEN_TINY, LEN_SHORT],
        "desc": (
            "Одне конкретне питання, на яке легко й хочеться відповісти. Часто"
            " автор сам дає свою коротку відповідь («Я почну: …»), і вона"
            " прямо відповідає на поставлене питання."
        ),
        "examples": [
            "Яка найдурніша покупка у вашому житті?\n\nЯ почну: костюм для спортзалу, який я одягнув рівно один раз.",
            "Скільки вкладок у вас відкрито в браузері прямо зараз? Чесно.\n\nУ мене 47.",
            "Яка порада від батьків виявилась правильною, хоч ви й сперечались?\n\nМоя: «Не бери кредит на телефон».",
            "Яка дрібниця у вашому дні робить вас щасливим?\n\nМене — теплий душ і чисті шкарпетки.",
            "Ви ще ходите в кіно чи вже тільки вдома з серіалом?\n\nЯ востаннє був у кінотеатрі рік тому і весь сеанс думав, що на дивані зручніше.",
            "Що найбільше дратує у дорослому житті?\n\nДля мене — що за все треба постійно платити, і воно ніколи не закінчується.",
        ],
    },
    {
        "name": "Побутове спостереження",
        "weight": 22,
        "lengths": [LEN_TINY, LEN_TINY, LEN_SHORT],
        "desc": (
            "Одна проста життєва думка, у якій люди впізнають себе. Самоіронічно,"
            " тепло, 1-3 речення. Смішно не через вигадане, а тому що правда."
        ),
        "examples": [
            "Колись я планував змінити світ. Тепер головне досягнення тижня — вчасно записався до стоматолога.",
            "Після 30 розумієш, що «давай швидко пообідаємо» — це мінімум дві години.",
            "Ліг спати о 23:00, щоб виспатись. О 01:30 дивлюсь відео про те, як будують мости.",
            "У телефоні три тисячі фото, а коли треба показати друзям одне, я гортаю хвилин десять.",
            "Вдома завжди є три речі: зарядка, яка нічого не заряджає, ключ невідомо від чого і пакет з пакетами.",
            "Спочатку ти молодий, а потім раптом болить спина від того, що ти надто різко встав.",
        ],
    },
    {
        "name": "Гарячий тейк",
        "weight": 14,
        "lengths": [LEN_TINY, LEN_SHORT],
        "desc": (
            "Різка, але зрозуміла думка про звички, моду на «саморозвиток»,"
            " гроші чи роботу. Одна теза, без списків. Можна закінчити"
            " «Згодні?»."
        ),
        "examples": [
            "Скажу непопулярне: «ранковий ритуал з 7 пунктів» — це просто спосіб відкласти початок роботи на дві години. Згодні?",
            "«Остання знижка року» буває щомісяця. Ми всі це знаємо і все одно купуємо.",
            "Більшість «курсів з нуля до 5000$» навчають головному: як продавати курси.",
            "Робота мрії — міф. Є нормальна робота, де платять вчасно і не заважають жити. І цього вже досить.",
            "Відпустка, з якої повертаєшся втомленішим, — це не відпустка, а турне.",
        ],
    },
    {
        "name": "Проста порада",
        "weight": 12,
        "lengths": [LEN_SHORT, LEN_SHORT, LEN_MEDIUM],
        "desc": (
            "Спокійна, впевнена порада простими словами про людей, гроші,"
            " друзів чи самоповагу. Тон чоловіка, який це пережив. Без"
            " повчань і без пафосу."
        ),
        "examples": [
            "Хороших людей не треба вмовляти залишитись.\n\nЯкщо довелось вмовляти — це вже не про почуття, а про зручність.",
            "Не відповідай одразу, коли злий. Виспись, випий води, і тільки потім пиши. Відповідь вийде іншою.",
            "Гроші, які позичаєш другові, краще одразу вважати подарунком. Якщо повернуть — приємний бонус.",
            "Не порівнюй свій перший рік із чужим десятим.",
            "Купуй менше речей, але кращої якості. Дешеві кросівки за пів року коштують дорожче за хороші за рік.",
            "Якщо після зустрічі з людиною ти щоразу почуваєшся виснаженим, це не збіг. Порахуй, скільки таких зустрічей було.",
        ],
    },
    {
        "name": "Математика життя",
        "weight": 10,
        "lengths": [LEN_MEDIUM],
        "desc": (
            "Короткий підрахунок із реальними цифрами (ціни, витрати, час):"
            " кілька коротких рядків і сухий висновок. Цифри мають сходитись"
            " за арифметикою."
        ),
        "examples": [
            "Проїзд — 20 грн в один бік.\nПо буднях за місяць виходить 880 грн.\n\nМісячний квиток коштує 700.\nЯ про нього знаю вже пів року.",
            "Підписки: фільми — 299, музика — 99, хмара — 59.\nРазом 457 грн на місяць.\n\nДивлюсь я при цьому той самий серіал уже втретє.",
            "Піца — 380 грн, доставка — 70.\nПродукти на таку саму піцу вдома — 150.\n\nАле готувати довелось би мені.",
            "Зарплата — 30 000.\nОренда 12 000, комуналка 2 500, їжа 9 000, транспорт 1 500.\n\nЗалишок — 5 000 на «все інше». І це я ще не хворів.",
            "Абонемент у зал — 900 грн.\nЯ був 3 рази.\n\nВиходить 300 грн за візит. Дорожче, ніж обід у ресторані.",
        ],
    },
    {
        "name": "Словник / переклад",
        "bait": False,
        "weight": 8,
        "lengths": [LEN_TINY, LEN_SHORT],
        "desc": "Формат «Словник…»: одна-дві фрази і їхній чесний переклад.",
        "examples": [
            "Словник клієнта:\n«Бюджет обговоримо пізніше»\n\nПереклад: «бюджету немає».",
            "Словник вихідного:\n«Полежу тільки п'ять хвилин»\n\nПереклад: «побачимось о 16:00».",
            "Словник відмазок:\n«Я не встигаю»\n\nПереклад: «я не хочу, але пояснювати не буду».",
            "Словник програміста:\n«У мене це працює»\n\nПереклад: «у тебе воно зламається».",
            "Словник дорослого життя:\n«Давай якось зустрінемось»\n\nПереклад: «побачимось на чиємусь весіллі».",
        ],
    },
    {
        "name": "Правила / список",
        "bait": False,
        "weight": 5,
        "lengths": [LEN_SHORT, LEN_MEDIUM],
        "desc": (
            "Короткий список із 3-4 пунктів на одну тему. Пункти не"
            " суперечать одне одному. Останній — найсмішніший."
        ),
        "examples": [
            "Правила фрілансера:\n1. Передоплата.\n2. Ще раз передоплата.\n3. «Це займе п'ять хвилин» = три дні.\n4. Дивись пункт 1.",
            "Правила дорослого життя:\n1. Ніколи не ходи в магазин голодним.\n2. Ніколи не відповідай на повідомлення, коли злий.\n3. Ніколи не кажи «я швидко», коли йдеш у магазин.\n4. Дивись пункт 1.",
            "Правила довгої дороги:\n1. Зарядити повербанк заздалегідь.\n2. Взяти їжу, навіть якщо «поїдемо без зупинок».\n3. Ти все одно зупинишся.",
        ],
    },
    {
        "name": "Колективна ностальгія",
        "weight": 5,
        "lengths": [LEN_TINY, LEN_SHORT],
        "desc": (
            "«А пам'ятаєте…» про дитинство, школу, 2000-ні, старий інтернет."
            " Тепла іронія, наприкінці питання або зітхання."
        ),
        "examples": [
            "А ви пам'ятаєте часи, коли відео «докачувалось» і ти йшов пити чай? Тоді в нас була витримка.",
            "Пам'ятаєте, як ми всі знали номери телефонів напам'ять? Тепер я не пам'ятаю власний.",
            "Пам'ятаєте, як щастя було — вийти з дому зранку і повернутись, коли стемніло, нікому нічого не повідомляючи?",
            "Пам'ятаєте, як у школі найстрашнішим було «до дошки»? Тепер найстрашніше — «нам треба поговорити».",
        ],
    },
    {
        "name": "Чесне зізнання",
        "weight": 6,
        "lengths": [LEN_SHORT],
        "desc": (
            "Короткий щирий пост про особисте (втома, друзі, самотність) без"
            " пафосу, як думка вголос. Закінчується просто, без висновків і"
            " без «цінуйте себе»."
        ),
        "examples": [
            "Помітив, що найкращі думки приходять у дорозі, коли немає що робити, крім як дивитись у вікно.",
            "Стало легше, коли зрозумів, що не обов'язково відповідати на всі повідомлення одразу. Світ не розвалився.",
            "Остання річ, яку хочеться чути, коли втомився, — «тримайся». Хочеться, щоб хтось просто сказав: «Давай я допоможу».",
            "Дружити з людьми, з якими не треба вдавати, — найбільша розкіш після 30.",
        ],
    },
]

BAIT_TYPES = [
    "Додай наприкінці ОДНЕ конкретне питання, яке прямо стосується теми поста (не загальне «А ви як?»).",
    "Наприкінці попроси читачів написати свій варіант у коментарях і дай свій короткий варіант як приклад.",
    "Заверши коротким «Згодні?» — лише якщо думка в пості спірна.",
    "Заверши питанням: у кого було так само? Попроси розповісти найкурйознішу деталь.",
]

PROVOKE_TYPES = [
    "Зроби думку трохи зухвалою, наче кидаєш виклик загальноприйнятому.",
    "Додай один «непопулярний» поворот, який змусить частину читачів погодитись, а частину — посперечатись.",
    "Зачепи популярний міф («треба рано вставати», «диплом — головне», «гроші не головне»).",
]

TOPICS = [
    "гроші та ціни", "зарплата й витрати", "фріланс і клієнти", "робота в офісі",
    "дедлайни", "будильник і ранок", "недосип і безсоння", "доставка їжі",
    "спортзал", "друзі, які пропали", "справжня дружба", "побачення",
    "довгі стосунки", "батьки й родичі", "дитинство", "школа",
    "універ і диплом", "громадський транспорт", "ремонт у квартирі", "сусіди",
    "магазин і каса", "лікарі й аналізи", "вік і «вже не двадцять»",
    "самотність", "соцмережі й лайки", "нейромережі", "програмісти й код",
    "комп'ютерні ігри", "серіали", "відпустка", "кіт чи собака",
    "прання й прибирання", "онлайн-курси", "подарунки", "відмазки",
    "дорослішання", "незручні ситуації", "маленькі радощі", "дивні звички",
    "інтернет-магазини", "повідомлення в месенджері", "голосові повідомлення",
    "телефон і зарядка", "Wi-Fi та інтернет", "холодильник і вечеря",
    "готування вдома", "кредити й борги", "накопичення", "знижки та акції",
    "переїзд", "орендна квартира", "дача і город", "літній відпочинок",
    "зима і холод", "погода", "сніданок", "обід на роботі", "корпоративи",
    "співбесіди", "начальник", "колеги", "понеділок", "п'ятниця", "вихідні",
    "новорічні свята", "день народження", "весілля друзів",
    "зустріч випускників", "старі фото", "навушники й музика", "плейлисти",
    "книжки, які купив і не прочитав", "кіно на вечір", "футбол по телевізору",
    "хобі, яке кинув", "підписки на сервіси", "розумні гаджети", "повербанк",
    "посилки й пошта", "кур'єри", "ліфт", "черги", "банківський застосунок",
    "забуті паролі", "плани на рік", "чайові", "парковка", "автомобіль і ремонт",
    "заправка", "стоматолог", "барбер і стрижка", "одяг і розпродажі", "взуття",
    "відгуки й рейтинги", "поради від друзів", "розмови по телефону",
    "режим дня", "ніч і думки перед сном", "іспити й сесія", "здача крові й довідки",
    "навігатор у дорозі", "ремонт техніки", "паперова тяганина", "прокрастинація",
]

TOPIC_HISTORY_FILE = BASE_DIR / "topics_used.json"
TOPIC_COOLDOWN = 30  # скільки останніх тем не повторювати


def pick_topic() -> str:
    """Обирає тему, якої не було серед останніх TOPIC_COOLDOWN."""
    used = load_json(TOPIC_HISTORY_FILE, [])
    fresh = [t for t in TOPICS if t not in used[-TOPIC_COOLDOWN:]]
    topic = random.choice(fresh or TOPICS)
    used.append(topic)
    save_json(TOPIC_HISTORY_FILE, used[-100:])
    return topic


# ───────────────────────── ДОПОМІЖНЕ ─────────────────────────


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def weighted_choice(items):
    return random.choices(items, weights=[s["weight"] for s in items], k=1)[0]


# ───────────────────────── ПРОМПТ ─────────────────────────


def build_prompt() -> tuple[str, str]:
    """Збирає унікальний промпт. Повертає (промпт, короткий опис мікса)."""
    style = weighted_choice(STYLES)

    second = None
    if random.random() < SECOND_STYLE_CHANCE:
        second = random.choice([s for s in STYLES if s is not style])

    topic = pick_topic()
    length = random.choice(style["lengths"])
    emoji_count = random.choice([0, 0, 0, 1, 1, 2])
    use_bait = style.get("bait", True) and random.random() < BAIT_CHANCE
    use_provoke = random.random() < PROVOKE_CHANCE
    recent = load_json(HISTORY_FILE, [])[-6:]
    examples = random.sample(style["examples"], min(3, len(style["examples"])))

    parts = [
        "Ти ведеш популярний український акаунт у Threads. Автор — чоловік"
        " 25-40 років, живе в Україні, пише від першої особи ЧОЛОВІЧОГО роду"
        " (я пішов, я зробив, я подумав). Пише просто, як говорить із друзями:"
        " коротко, без прикрас, з сухою самоіронією, без пафосу і без повчань.",
        "",
        "ЗАВДАННЯ: напиши ОДИН новий пост для Threads.",
        "",
        "ПРАВИЛА:",
        "- Пиши ВИКЛЮЧНО українською мовою, без суржику і російських слів.",
        "- Одразу текст поста: без вступів, заголовків і пояснень.",
        "- Без хештегів, без markdown (жодних зірочок і решіток), без лапок"
        " навколо всього поста.",
        "- Без нецензурної лексики, без війни, політики, релігії, без образ"
        " людей за статтю, національністю чи поглядами.",
        "- ОДНА думка на пост. Не пхай у пост усе одразу: достатньо одного"
        " спостереження або однієї ідеї.",
        "- Пиши про те, що справді буває в житті, і де люди впізнають себе."
        " Не вигадуй абсурд, дивні порівняння і «розумні» метафори. Проста"
        " фраза краще за кучеряву.",
        "- Усе має бути логічним: нічого не суперечить одне одному, жарт"
        " зрозумілий з першого прочитання, цифри сходяться.",
        "- Конкретика тільки там, де вона природна (ціна, час, назва). Не"
        " обов'язково її додавати.",
        f"- Довжина: {length}. Не більше 400 символів разом з пробілами.",
        f"- Емодзі: рівно {emoji_count}"
        + (" (не в кожному рядку)." if emoji_count else "."),
        "- Не копіюй чужі пости й відомі жарти. Приклади нижче — тільки для"
        " інтонації.",
        "",
        f"ФОРМАТ: «{style['name']}» — {style['desc']}",
        f"ТЕМА ПОСТА (обов'язково): {topic}. Пиши саме про неї і не підміняй її"
        " кавою, таксі чи іншими «улюбленими» побутовими сюжетами.",
        "",
        "ПРИКЛАДИ ТОНУ (так звучать сильні пости цього формату; перейми"
        " простоту й інтонацію, але НЕ повторюй їхні сюжети чи слова):",
    ]
    for ex in examples:
        parts += ["---", ex]
    parts.append("---")

    if second:
        parts += [
            "",
            f"Можеш трохи підмішати нотку формату «{second['name']}»:"
            f" {second['desc']}",
        ]

    if use_bait:
        parts += ["", "ПРИЙОМ НА КОМЕНТАРІ: " + random.choice(BAIT_TYPES)]

    if use_provoke:
        parts += ["", "ЛЕГКА ПРОВОКАЦІЯ: " + random.choice(PROVOKE_TYPES)]

    if recent:
        parts += [
            "",
            "НЕДАВНІ ПОСТИ (не повторюй їхні теми й побудову):",
        ]
        parts += [f"- {r[:120].replace(chr(10), ' ')}…" for r in recent]

    meta = (
        f"стиль={style['name']}"
        + (f" + {second['name']}" if second else "")
        + f" | тема={topic}"
        + (" | байт" if use_bait else "")
        + (" | провокація" if use_provoke else "")
    )
    return "\n".join(parts), meta


# ───────────────────────── GEMINI ─────────────────────────

_client = None
_models_cache = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def get_candidate_models() -> list[str]:
    global _models_cache
    if _models_cache is not None:
        return _models_cache

    candidates = list(PREFERRED_MODELS)
    try:
        bad = ("tts", "image", "audio", "live", "embedding", "robotics",
               "computer", "thinking", "learnlm")
        for m in get_client().models.list():
            name = (m.name or "").replace("models/", "")
            if (
                "flash" in name
                and name not in candidates
                and not any(b in name for b in bad)
            ):
                candidates.append(name)
    except Exception as e:
        print(f"⚠️ Не вдалося отримати список моделей: {e}")

    _models_cache = candidates
    return candidates


def clean_text(text: str) -> str:
    text = text.replace("**", "").replace("__", "").strip()
    if len(text) > 1 and text[0] in '"«“' and text[-1] in '"»”':
        text = text[1:-1].strip()
    return text


def trim_to_limit(text: str, limit: int = THREADS_LIMIT) -> str:
    """Обрізає текст до ліміту Threads по межі речення."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = max(cut.rfind(c) for c in ".!?…")
    if best > limit * 0.5:
        return cut[: best + 1].rstrip()
    return cut.rstrip()


def generate_post(prompt: str, temperature: float = 1.0) -> str:
    """Генерує текст з повторами та запасними моделями."""
    config_gen = types.GenerateContentConfig(
        temperature=temperature,
        http_options=types.HttpOptions(timeout=30000),  # 30 секунд на відповідь
    )

    for idx, model in enumerate(get_candidate_models()):
        retries = FIRST_MODEL_RETRIES if idx == 0 else RETRIES_PER_MODEL
        for attempt in range(1, retries + 1):
            try:
                print(f"➡️  Модель: {model} (спроба {attempt}/{retries})")
                response = get_client().models.generate_content(
                    model=model, contents=prompt, config=config_gen
                )
                text = clean_text(response.text or "")
                if not text:
                    print("⚠️ Порожня відповідь, пробуємо ще раз...")
                    continue
                return trim_to_limit(text)

            except errors.ServerError as e:
                wait = min(5 * attempt, 15)
                print(f"⚠️ {model}: помилка {e.code}, чекаю {wait} с...")
                time.sleep(wait)

            except errors.ClientError as e:
                print(f"❌ {model}: помилка {e.code} — {e.message}")
                if e.code == 429:
                    time.sleep(10)
                break

            except Exception as e:
                print(
                    f"⏱️ {model}: немає відповіді або збій ({type(e).__name__}),"
                    " беремо наступну модель..."
                )
                break

    raise RuntimeError("Усі моделі зараз недоступні, спробуй пізніше.")


# ───────────────────────── РЕДАКТОР ─────────────────────────

REVIEW_PROMPT = (
    "Ти суворий редактор українського акаунту в Threads. Автор поста — чоловік."
    " Уважно перевір пост:\n"
    "1. Чи немає суперечностей: пункти, репліки й висновок не мають"
    " суперечити одне одному (наприклад, «без батарейок» і далі «робот»).\n"
    "2. Чи питання або «Я почну:» в кінці прямо випливає з теми поста.\n"
    "3. Чи зрозуміло, хто що говорить і що відбувається.\n"
    "4. Чи жарт зрозумілий з першого прочитання, а цифри сходяться.\n"
    "5. Чи звучить пост як жива людина, а не штучно «розумно»: якщо фрази"
    " кучеряві або дивні — перепиши простіше.\n"
    "6. Чи правильна українська мова й чоловічий рід автора.\n\n"
    "Якщо все добре — поверни пост БЕЗ змін. Якщо є проблеми — перепиши так,"
    " щоб усе було логічно й просто, зберігши ідею та стиль (не довше за"
    " оригінал). Якщо пост неможливо врятувати — відповідай одним словом:"
    " ПЕРЕГЕНЕРУВАТИ.\n"
    "Поверни ТІЛЬКИ текст поста, без пояснень і без лапок.\n\n"
    "ПОСТ:\n---\n{post}\n---"
)


def review_post(text: str) -> str | None:
    """Редактор перевіряє логіку. Повертає текст або None, якщо пост поганий."""
    try:
        edited = generate_post(REVIEW_PROMPT.format(post=text), temperature=0.3)
    except Exception as e:
        print(f"⚠️ Редактор недоступний ({e}), лишаю пост як є.")
        return text
    if "ПЕРЕГЕНЕРУВАТИ" in edited.upper():
        return None
    return edited


def create_post(max_tries: int = 3) -> tuple[str, str]:
    """Генерує пост і перевіряє його редактором. Повертає (текст, опис мікса)."""
    last_text, last_meta = "", ""
    for _ in range(max_tries):
        prompt, meta = build_prompt()
        print(f"🎲 Мікс: {meta}")
        text = generate_post(prompt)
        print("🧐 Редактор перевіряє логіку...")
        checked = review_post(text)
        if checked:
            return checked, meta
        print("♻️ Редактор забракував пост, генерую новий...")
        last_text, last_meta = text, meta
    return last_text, last_meta


def remember(text: str):
    history = load_json(HISTORY_FILE, [])
    history.append(text)
    save_json(HISTORY_FILE, history[-HISTORY_SIZE:])


# ───────────────────────── THREADS ─────────────────────────


def publish_to_threads(text: str) -> bool:
    """Публікує текст у Threads у два кроки. Повертає True, якщо успішно."""
    me_res = requests.get(
        "https://graph.threads.net/v1.0/me",
        params={"access_token": config.THREADS_ACCESS_TOKEN, "fields": "id"},
        timeout=30,
    ).json()
    user_id = me_res.get("id")

    if not user_id:
        print("❌ Не вдалося отримати ID користувача Threads:", me_res)
        return False

    print(f"👤 Успішний вхід в акаунт Threads (ID: {user_id})")

    print("📦 Створюємо контейнер поста...")
    container_res = requests.post(
        f"https://graph.threads.net/v1.0/{user_id}/threads",
        data={
            "media_type": "TEXT",
            "text": text,
            "access_token": config.THREADS_ACCESS_TOKEN,
        },
        timeout=30,
    ).json()
    print("🔍 Відповідь при створенні контейнера:", container_res)

    creation_id = container_res.get("id")
    if not creation_id:
        print("❌ Помилка при створенні контейнера")
        return False

    print("⏳ Чекаємо 30 секунд перед публікацією...")
    time.sleep(30)

    print("🚀 Відправляємо пост у Threads...")
    publish_res = requests.post(
        f"https://graph.threads.net/v1.0/{user_id}/threads_publish",
        data={
            "creation_id": creation_id,
            "access_token": config.THREADS_ACCESS_TOKEN,
        },
        timeout=30,
    ).json()
    print("🔍 Відповідь при публікації:", publish_res)

    if "id" in publish_res:
        print(f"✅ Готово! Пост опубліковано. ID поста: {publish_res['id']}")
        return True

    print("❌ Помилка при публікації")
    return False


# ───────────────────────── ОДИН ПОСТ ─────────────────────────


def make_and_publish(retries: int = 3, pause_min: int = 5) -> bool:
    for attempt in range(1, retries + 1):
        try:
            text, _meta = create_post()
            remember(text)
            print(
                f"\n--- Пост ({len(text)} симв.) ---\n{text}\n"
                "---------------------------\n"
            )
            if publish_to_threads(text):
                return True
        except Exception as e:
            print(f"❌ Помилка (спроба {attempt}/{retries}): {e}")

        if attempt < retries:
            print(f"🔁 Повторю через {pause_min} хв...")
            time.sleep(pause_min * 60)
    return False


def preview(count: int):
    for i in range(1, count + 1):
        print(f"\n===== Тестовий пост {i}/{count} =====")
        text, _meta = create_post()
        print(f"\n{text}\n({len(text)} симв.)")


# ───────────────────────── РОЗКЛАД: 4 ПОСТИ НА ДОБУ ─────────────────────────


def sleep_until(target: datetime):
    while True:
        left = (target - now_kyiv()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))


def run_scheduler():
    print("🕒 Планувальник запущено: 4 пости на добу, 09:00-24:00 (Київ).")
    print("   Щоб зупинити — натисни Ctrl+C.\n")

    while True:
        now = now_kyiv()
        today = now.strftime("%Y-%m-%d")
        state = load_json(STATE_FILE, {})

        if state.get("date") != today or not state.get("plan"):
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            plan = [
                (day_start + timedelta(minutes=random.randint(a, b))).isoformat()
                for a, b in SLOT_WINDOWS
            ]
            state = {"date": today, "plan": plan, "done": []}
            save_json(STATE_FILE, state)
            times = ", ".join(datetime.fromisoformat(p).strftime("%H:%M") for p in plan)
            print(f"📅 План на {today}: {times}")

        for i, iso in enumerate(state["plan"]):
            if i in state["done"]:
                continue

            target = datetime.fromisoformat(iso)
            if target < now_kyiv() - timedelta(minutes=60):
                print(f"⏭️  Слот {target:%H:%M} пропущено (час минув).")
                state["done"].append(i)
                save_json(STATE_FILE, state)
                continue

            if target > now_kyiv():
                print(f"⏰ Наступний пост о {target:%H:%M}")
                sleep_until(target)

            print(f"\n=== Публікація (слот {i + 1}/4) ===")
            ok = make_and_publish()
            if not ok:
                print("⚠️ Не вдалося опублікувати, слот пропущено.")
            state["done"].append(i)
            save_json(STATE_FILE, state)

        tomorrow = (now_kyiv() + timedelta(days=1)).replace(
            hour=0, minute=1, second=0, microsecond=0
        )
        print(f"😴 Усе на сьогодні. Прокинусь о {tomorrow:%d.%m %H:%M}")
        sleep_until(tomorrow)


# ───────────────────────── ТОЧКА ВХОДУ ─────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Threads-бот зі стилями")
    parser.add_argument(
        "--once", action="store_true", help="опублікувати один пост зараз"
    )
    parser.add_argument(
        "--preview",
        nargs="?",
        const=3,
        type=int,
        metavar="N",
        help="показати N тестових постів без публікації (за замовч. 3)",
    )
    args = parser.parse_args()

    if args.preview:
        preview(args.preview)
    elif args.once:
        make_and_publish()
    else:
        run_scheduler()