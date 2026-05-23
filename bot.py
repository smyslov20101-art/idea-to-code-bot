"""
Telegram-бот: фабрика планов и промптов (Фаза A).

Что делает:
  /new -> присылаешь идею -> бот возвращает план -> ты одобряешь или просишь переделать
  -> бот выдаёт промпты по шагам отдельными сообщениями.

LLM-вызовы пока заглушки. Когда подключим Claude API — меняем только функции
generate_plan() и generate_prompts(), остальной код не трогаем.
"""

import asyncio
import io
import logging
import os
import re
import zipfile
from datetime import datetime
from html import escape
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


# ──────────────────────────── Конфиг ────────────────────────────

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
PROXYAPI_KEY = os.getenv("PROXYAPI_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# proxyapi.ru — российский посредник, формат API идентичен Anthropic,
# поэтому работает официальный SDK с подменённым base_url.
PROXYAPI_BASE_URL = "https://api.proxyapi.ru/anthropic"

if not BOT_TOKEN:
    raise SystemExit(
        "В .env нет TELEGRAM_BOT_TOKEN. Открой файл .env в проекте, вставь токен "
        "от @BotFather между кавычек и перезапусти бота."
    )

HAS_LLM = bool(PROXYAPI_KEY or ANTHROPIC_KEY)

# Клиент Claude. Если ключ proxyapi есть — ходим через него (РФ-friendly).
# Иначе, если есть прямой ключ Anthropic — напрямую. Нет ключей — заглушки.
if PROXYAPI_KEY:
    claude = AsyncAnthropic(api_key=PROXYAPI_KEY, base_url=PROXYAPI_BASE_URL)
elif ANTHROPIC_KEY:
    claude = AsyncAnthropic(api_key=ANTHROPIC_KEY)
else:
    claude = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bot")


# ──────────────────────────── Состояния ────────────────────────────

class Flow(StatesGroup):
    waiting_for_idea = State()
    reviewing_plan = State()
    editing_plan = State()  # ждём текст правок к плану
    deciding = State()     # отвечаем на развилки ПРЕДЛОЖЕНИЯ по одной
    categorized = State()  # план одобрен, разложен по отделам, можно смотреть
    redo_feedback = State()  # ждём текст замечания для переделки отдела


# ──────────────────────────── LLM (пока заглушки) ────────────────────────────

PLAN_DELIM = "===ПОДРОБНО==="
CAT_PREFIX = "===КАТЕГОРИЯ:"

# Отделы. Порядок важен — в нём показываем сводку и кнопки.
CATEGORIES = ["ДИЗАЙН", "КОД", "МУЗЫКА", "НАПОЛНЕНИЕ", "ПРЕДЛОЖЕНИЯ", "ДРУГИЕ ВОПРОСЫ"]
CAT_EMOJI = {
    "ДИЗАЙН": "🎨",
    "КОД": "💻",
    "МУЗЫКА": "🎵",
    "НАПОЛНЕНИЕ": "📝",
    "ПРЕДЛОЖЕНИЯ": "💡",
    "ДРУГИЕ ВОПРОСЫ": "❓",
}

PLAN_SYSTEM = (
    "Ты — фабрика идей и планов. Пользователь кидает идею в свободной форме. "
    "Отвечай по-русски, обычным текстом (не markdown-таблицы) — ответ уйдёт в Telegram. "
    "Не задавай уточняющих вопросов — разумно додумай недостающее.\n\n"
    "Формат ответа СТРОГО такой:\n\n"
    "СНАЧАЛА краткий обзор для занятого руководителя:\n"
    "• 1 строка — цель проекта простыми словами.\n"
    "• 3–7 пунктов ключевых этапов (по одной короткой строке, без подпунктов, без воды).\n"
    "Это всё, что человек увидит сразу — он должен за 10 секунд понять что будет делаться.\n\n"
    f"ЗАТЕМ ровно строка-разделитель: {PLAN_DELIM}\n\n"
    "ЗАТЕМ полный детальный план: цель, ограничения, желаемый результат, "
    "пошаговый план с подшагами (1, 1.1, 1.2, 2, ...). Здесь можно подробно."
)

CATEGORIZE_SYSTEM = (
    "Ты — диспетчер проекта. На вход — полный план со шагами и подшагами. "
    "Разнеси ВСЕ шаги и подшаги по отделам:\n"
    "ДИЗАЙН, КОД, МУЗЫКА, НАПОЛНЕНИЕ (тексты/контент/данные), ПРЕДЛОЖЕНИЯ "
    "(идеи и решения, требующие выбора руководителя).\n"
    "Шаги, не подходящие ни в один отдел — в «ДРУГИЕ ВОПРОСЫ».\n\n"
    "Для каждого НЕПУСТОГО отдела собери ОДИН готовый промпт-ТЗ: "
    "пронумерованный список конкретных задач этого отдела (1), 2), 3)...), "
    "каждая задача самодостаточна и сразу исполнима профильным специалистом, "
    "без отсылок 'см. план'. Пиши по-русски, по делу.\n\n"
    "ВАЖНО: если ниже даны «Уже принятые решения руководителя» — НЕ повторяй "
    "эти вопросы в ПРЕДЛОЖЕНИЯ (они уже решены). Наоборот: используй выбранные "
    "варианты как данность в ТЗ остальных отделов (КОД/ДИЗАЙН/…).\n\n"
    "Формат ответа СТРОГО: для каждого непустого отдела сначала строка-разделитель\n"
    f"{CAT_PREFIX} ИМЯ_ОТДЕЛА===\n"
    "затем сам промпт-ТЗ. Пустые отделы полностью пропускай (не упоминай). "
    "Никакого текста вне этой структуры."
)


def _text(resp) -> str:
    """Достаёт текст из ответа Claude (склеивает все текстовые блоки)."""
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _split_plan(raw: str) -> tuple[str, str]:
    """Режет ответ модели на (краткий обзор, полный план) по PLAN_DELIM.
    Если разделителя нет — обзор и полный план совпадают (деградируем мягко)."""
    if PLAN_DELIM in raw:
        overview, full = raw.split(PLAN_DELIM, 1)
        overview, full = overview.strip(), full.strip()
        if overview and full:
            return overview, full
    return raw, raw


async def generate_plan(
    idea: str, previous_attempt: str | None = None
) -> tuple[str, str]:
    """Принимает идею (и опц. предыдущий полный план, если 'переделать').
    Возвращает кортеж (краткий обзор, полный детальный план)."""
    if not HAS_LLM:
        retry_note = " (вариант 2 — другой угол)" if previous_attempt else ""
        stub = (
            f"🔧 Заглушка{retry_note}. Ключа API нет — здесь был бы настоящий план.\n\n"
            f"━━━ ТВОЯ ИДЕЯ ━━━\n"
            f"{idea[:300]}{'…' if len(idea) > 300 else ''}"
        )
        return stub, stub

    user_msg = f"Идея:\n{idea}"
    if previous_attempt:
        user_msg += (
            f"\n\n---\nПредыдущий план НЕ подошёл, его отвергли. "
            f"Зайди с другого угла, не повторяй структуру:\n{previous_attempt}"
        )
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2500,
            system=PLAN_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = _text(resp) or "Пустой ответ от модели. Попробуй ещё раз."
        return _split_plan(raw)
    except Exception as e:
        log.exception("Ошибка вызова Claude в generate_plan")
        err = f"⚠️ Ошибка обращения к Claude: {e}\n\nПроверь ключ/баланс на proxyapi.ru."
        return err, err


EDIT_SYSTEM = (
    "Тебе дан готовый план и запрос правок от руководителя "
    "(убрать/изменить/добавить конкретные пункты или подпункты).\n"
    "Внеси ТОЛЬКО запрошенные изменения. Всё остальное сохрани как было, "
    "не переписывай и не переставляй. Перенумеруй пункты если что-то удалил.\n\n"
    "Верни в ТОМ ЖЕ формате: краткий обзор (цель + 3–7 пунктов), затем "
    f"строка-разделитель {PLAN_DELIM}, затем полный детальный план."
)


async def edit_plan(full_plan: str, edit_request: str) -> tuple[str, str]:
    """Применяет точечные правки к плану. Возвращает (обзор, полный)."""
    if not HAS_LLM:
        return full_plan, full_plan
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2500,
            system=EDIT_SYSTEM,
            messages=[{"role": "user", "content": (
                f"Текущий план:\n{full_plan}\n\n---\n"
                f"Правки руководителя:\n{edit_request}"
            )}],
        )
        raw = _text(resp) or "Пустой ответ. Попробуй сформулировать правку иначе."
        return _split_plan(raw)
    except Exception as e:
        log.exception("Ошибка вызова Claude в edit_plan")
        err = f"⚠️ Ошибка обращения к Claude: {e}\n\nПроверь ключ/баланс на proxyapi.ru."
        return err, err


async def explain_options(question: str, opts: list[str], plan: str) -> str:
    """Объясняет варианты развилки простыми словами для нетех-руководителя."""
    if not HAS_LLM:
        return "🔧 Заглушка — ключа API нет, объяснить нечем."
    sys = (
        "Объясни нетехническому руководителю простыми словами, без жаргона, "
        "что значит каждый вариант: суть, плюсы и минусы, кому что подходит. "
        "В конце — одна строка «Обычно выбирают: …». Коротко, по делу."
    )
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            system=sys,
            messages=[{"role": "user", "content": (
                f"Контекст проекта:\n{plan[:1500]}\n\n"
                f"Вопрос: {question}\nВарианты: {' | '.join(opts)}"
            )}],
        )
        return _text(resp) or "Не удалось объяснить, попробуй ещё раз."
    except Exception as e:
        log.exception("Ошибка вызова Claude в explain_options")
        return f"⚠️ Ошибка обращения к Claude: {e}"


async def auto_pick(question: str, opts: list[str], plan: str) -> int:
    """Бот сам выбирает лучший вариант. Возвращает индекс (0..N-1)."""
    if not HAS_LLM:
        return 0
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(opts))
    sys = (
        "Ты опытный техлид. Выбери ОДИН оптимальный вариант для этого проекта. "
        "Ответь ТОЛЬКО числом — номером варианта. Ничего больше."
    )
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            system=sys,
            messages=[{"role": "user", "content": (
                f"Контекст:\n{plan[:1500]}\n\nВопрос: {question}\n{numbered}"
            )}],
        )
        m = re.search(r"\d+", _text(resp))
        if m:
            j = int(m.group()) - 1
            if 0 <= j < len(opts):
                return j
    except Exception:
        log.exception("Ошибка вызова Claude в auto_pick")
    return 0


_CAT_SPLIT_RE = re.compile(rf"{re.escape(CAT_PREFIX)}\s*(.+?)\s*===")


def _parse_categories(raw: str) -> dict[str, str]:
    """Режет ответ модели на {ОТДЕЛ: промпт-ТЗ}, в порядке CATEGORIES.
    Имена нормализуются, мусорные/пустые отбрасываются."""
    parts = _CAT_SPLIT_RE.split(raw)
    # parts = [мусор_до, имя1, тело1, имя2, тело2, ...]
    found: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].strip().upper()
        body = parts[i + 1].strip()
        if name in CATEGORIES and body:
            found[name] = (found.get(name, "") + "\n\n" + body).strip()
    # упорядочиваем по CATEGORIES
    return {c: found[c] for c in CATEGORIES if c in found}


async def categorize_plan(
    plan: str, decisions: list | None = None
) -> dict[str, str]:
    """По одобренному ПОЛНОМУ плану — собирает один промпт-ТЗ на каждый
    непустой отдел. С учётом уже принятых решений (если переданы).
    Спецключ '__error__' при сбое API."""
    if not HAS_LLM:
        return {"ДРУГИЕ ВОПРОСЫ": "📋 Заглушка — ключа API нет, разнести по отделам нечем."}

    content = f"Полный план:\n{plan}"
    if decisions:
        decided = "\n".join(f"- {q}: {ans}" for q, ans in decisions)
        content += f"\n\n---\nУже принятые решения руководителя:\n{decided}"
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=5000,
            system=CATEGORIZE_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        cats = _parse_categories(_text(resp))
        if not cats:  # модель не дала разделителей — кладём всё в «другое»
            return {"ДРУГИЕ ВОПРОСЫ": _text(resp) or "Модель вернула пусто."}
        return cats
    except Exception as e:
        log.exception("Ошибка вызова Claude в categorize_plan")
        return {"__error__": f"⚠️ Ошибка обращения к Claude: {e}\n\nПроверь ключ/баланс на proxyapi.ru."}


PROJECTS_DIR = Path(__file__).parent / "projects"


def _slug(text: str) -> str:
    """Короткий безопасный кусок имени папки из идеи."""
    first = text.strip().splitlines()[0] if text.strip() else "проект"
    first = re.sub(r"[^\w\s-]", "", first, flags=re.UNICODE)[:40].strip()
    return re.sub(r"\s+", "_", first) or "проект"


def save_project(idea: str, full_plan: str, cats: dict[str, str]) -> Path:
    """Создаёт папку проекта и раскладывает по файлам: идея, план, отделы.
    Возвращает путь к папке."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    folder = PROJECTS_DIR / f"{stamp}_{_slug(idea)}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "_ИДЕЯ.txt").write_text(idea, encoding="utf-8")
    (folder / "_ПЛАН.md").write_text(full_plan, encoding="utf-8")
    for name, body in cats.items():
        fname = name.replace(" ", "_") + ".md"
        (folder / fname).write_text(body, encoding="utf-8")
    return folder


def list_projects() -> list[Path]:
    """Папки проектов, новые сверху."""
    if not PROJECTS_DIR.exists():
        return []
    folders = [p for p in PROJECTS_DIR.iterdir() if p.is_dir()]
    return sorted(folders, key=lambda p: p.stat().st_mtime, reverse=True)


def load_project(folder: Path) -> dict:
    """Восстанавливает проект из папки: idea, full_plan, cats."""
    idea = ""
    full_plan = ""
    if (folder / "_ИДЕЯ.txt").exists():
        idea = (folder / "_ИДЕЯ.txt").read_text(encoding="utf-8")
    if (folder / "_ПЛАН.md").exists():
        full_plan = (folder / "_ПЛАН.md").read_text(encoding="utf-8")
    cats: dict[str, str] = {}
    for f in folder.glob("*.md"):
        name = f.stem.replace("_", " ").upper()
        if name in CATEGORIES:
            cats[name] = f.read_text(encoding="utf-8")
    cats = {c: cats[c] for c in CATEGORIES if c in cats}  # порядок
    return {"idea": idea, "full_plan": full_plan, "cats": cats}


def executed_set(folder: Path) -> set[str]:
    """Отделы, у которых уже есть <ОТДЕЛ>_РЕЗУЛЬТАТ.md в папке проекта."""
    done: set[str] = set()
    if not folder or not folder.exists():
        return done
    for cat in CATEGORIES:
        if (folder / f"{cat.replace(' ', '_')}_РЕЗУЛЬТАТ.md").exists():
            done.add(cat)
    return done


# ──────────────────────────── Исполнение отделов ────────────────────────────

# Роли исполнителей. ПРЕДЛОЖЕНИЯ тут НЕТ — оно уже решено на развилках.
EXEC_ROLES = {
    "КОД": (
        "Ты senior-разработчик. Выдай ПОЛНЫЙ рабочий код, без сокращений и "
        "без '...здесь остальное'. Чтобы влезть — минимизируй декоративные "
        "комментарии и по возможности уложи всё в ОДИН файл (напр. main.py).\n"
        "Перед каждым файлом — строка с его именем в обратных кавычках, "
        "например `main.py`, затем код в ``` блоке с указанием языка (```python). "
        "Не пиши длинных пояснений до/после — только имя файла и код."
    ),
    "ДИЗАЙН": (
        "Ты UI/UX-дизайнер. Дай детальный ТЕКСТОВЫЙ бриф: экраны, структура, "
        "компоненты, цвета, типографика, состояния. Картинки НЕ генерируй — "
        "только описание, по которому дизайнер/нейросеть сделает макет."
    ),
    "МУЗЫКА": (
        "Ты саунд-дизайнер. Дай бриф: стиль, настроение, референсы, структура "
        "и список нужных треков/звуков. Аудио НЕ генерируй — только описание."
    ),
    "НАПОЛНЕНИЕ": (
        "Ты копирайтер/контент-мейкер. Выдай ГОТОВЫЕ финальные тексты и "
        "контент по ТЗ — то, что можно сразу вставлять в продукт."
    ),
    "ДРУГИЕ ВОПРОСЫ": (
        "Ответь конкретно и по существу на каждый пункт ТЗ, с практическими "
        "рекомендациями. Без воды."
    ),
}

EXEC_BASE = (
    "Работай максимально конкретно и готово к использованию. По-русски. "
    "Учитывай общий план и решения руководителя как данность, не переспрашивай."
)


async def execute_department(
    name: str, task: str, plan: str = "", decisions_text: str = "",
    feedback: str = "",
) -> str:
    """Гоняет ТЗ отдела через Claude с ролевым промптом → текст результата."""
    if not HAS_LLM:
        return "🔧 Заглушка — ключа API нет, исполнить нечем."
    role = EXEC_ROLES.get(name, "Выполни ТЗ по существу, по-русски.")
    content = f"ТЗ отдела «{name}»:\n{task}"
    if plan:
        content += f"\n\n---\nОбщий план проекта (контекст):\n{plan[:2000]}"
    if decisions_text:
        content += f"\n\n---\nРешения руководителя:\n{decisions_text[:1000]}"
    if feedback:
        content += (
            f"\n\n---\nПредыдущий результат НЕ подошёл. "
            f"Замечание руководителя: {feedback}\nПеределай с учётом замечания."
        )
    # КОД часто длинный — даём заметно больше токенов, чтобы не обрывался
    max_tok = 20000 if name == "КОД" else 8000
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tok,
            system=f"{role}\n\n{EXEC_BASE}",
            messages=[{"role": "user", "content": content}],
        )
        return _text(resp) or "Пустой ответ от модели. Попробуй ещё раз."
    except Exception as e:
        log.exception("Ошибка вызова Claude в execute_department")
        return f"⚠️ Ошибка обращения к Claude: {e}\n\nПроверь ключ/баланс на proxyapi.ru."


async def summarize_result(name: str, result: str) -> str:
    """Очень короткая сводка результата отдела для нетех-руководителя."""
    if not HAS_LLM:
        return "Результат готов — смотри файл."
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=350,
            system=(
                "Сделай ОЧЕНЬ короткую сводку для нетехнического руководителя: "
                "что конкретно сделано/получено. 3–5 пунктов, простыми словами, "
                "без кода и жаргона. Это анонс к файлу с полным результатом."
            ),
            messages=[{"role": "user", "content": f"Отдел: {name}\n\n{result[:6000]}"}],
        )
        return _text(resp) or "Результат готов — смотри файл."
    except Exception:
        log.exception("Ошибка вызова Claude в summarize_result")
        return "Результат готов — смотри файл."


_FENCE_RE = re.compile(r"^```[ \t]*([\w.+-]*)[ \t]*$", re.MULTILINE)
_FNAME_RE = re.compile(r"`([\w./-]+\.\w+)`|^#+\s*([\w./-]+\.\w+)\s*$", re.MULTILINE)
_LANG_EXT = {
    "python": "py", "py": "py", "json": "json", "bash": "sh", "sh": "sh",
    "bat": "bat", "html": "html", "css": "css", "javascript": "js", "js": "js",
    "c": "c", "cpp": "cpp", "java": "java", "ts": "ts", "go": "go",
    "rust": "rs", "sql": "sql", "yaml": "yaml", "toml": "toml", "ini": "ini",
}


def _looks_like_tree(code: str) -> bool:
    """Псевдографика дерева папок, а не код."""
    return sum(code.count(c) for c in "├└│─") >= 3


def extract_code_files(md: str) -> tuple[dict[str, str], bool]:
    """Достаёт файлы кода из markdown. Имя файла берётся из `name.ext`
    или заголовка перед блоком. Незакрытый последний блок (обрыв генерации)
    тоже берётся — до конца текста. Дерево папок/проза не считаются файлом.
    Возвращает ({имя: код}, был_ли_обрыв)."""
    files: dict[str, str] = {}
    fences = list(_FENCE_RE.finditer(md))
    auto = 0
    truncated = False
    i = 0
    while i < len(fences):
        open_m = fences[i]
        lang = (open_m.group(1) or "").lower()
        if i + 1 < len(fences):
            code = md[open_m.end():fences[i + 1].start()].strip("\n")
            i += 2
        else:  # незакрытый блок — генерация оборвалась
            code = md[open_m.end():].strip("\n")
            truncated = True
            i += 1
        names = _FNAME_RE.findall(md[:open_m.start()][-200:])
        fname = ""
        if names:
            fname = (names[-1][0] or names[-1][1]).strip().lstrip("./")
        if _looks_like_tree(code) and lang not in _LANG_EXT:
            continue  # дерево папок — не файл
        if not fname:
            if lang not in _LANG_EXT or len(code) < 40:
                continue  # проза/мелкий сниппет без имени — пропуск
            auto += 1
            fname = f"code_{auto}.{_LANG_EXT.get(lang, 'txt')}"
        files[fname] = (files[fname] + "\n" + code) if fname in files else code
    return files, truncated


# ──────────────────────────── Развилки (решения руководителя) ────────────────────────────

DEC_DELIM = "===РЕШЕНИЕ==="

DECISIONS_SYSTEM = (
    "Ты — аналитик проекта. На вход — полный план. Выдели ТОЛЬКО развилки, "
    "где реально нужен выбор руководителя (платформа, хранилище, технология, "
    "объём MVP, формат и т.п.). Не выдумывай развилок, которых нет в плане.\n"
    "Для каждой развилки — короткий вопрос и 2–4 КОРОТКИХ варианта "
    "(метка 2–6 слов, без длинных описаний).\n\n"
    "Если развилок нет — ответь одним словом: НЕТ\n\n"
    "Иначе формат СТРОГО, для каждой развилки:\n"
    f"{DEC_DELIM}\n"
    "ВОПРОС: <короткий вопрос>\n"
    "ВАРИАНТ: <короткая метка>\n"
    "ВАРИАНТ: <короткая метка>\n"
    "Никакого текста вне этой структуры."
)


def _parse_decisions(raw: str) -> list[dict]:
    """[{q, opts:[...]}] из ответа модели. Блоки без вопроса или с <2
    вариантами отбрасываются, вариантов не больше 4."""
    out: list[dict] = []
    for block in raw.split(DEC_DELIM):
        block = block.strip()
        if not block:
            continue
        q = ""
        opts: list[str] = []
        for line in block.splitlines():
            line = line.strip()
            if line.upper().startswith("ВОПРОС:"):
                q = line.split(":", 1)[1].strip()
            elif line.upper().startswith("ВАРИАНТ:"):
                opt = line.split(":", 1)[1].strip()
                if opt:
                    opts.append(opt)
        if q and len(opts) >= 2:
            out.append({"q": q, "opts": opts[:4]})
    return out


async def extract_decisions(plan: str) -> list[dict]:
    """Развилки, требующие выбора руководителя. Пусто — если их нет
    или API недоступен (фича опциональная, поток не блокирует)."""
    if not HAS_LLM:
        return []
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=DECISIONS_SYSTEM,
            messages=[{"role": "user", "content": f"Полный план:\n{plan}"}],
        )
        txt = _text(resp)
        if DEC_DELIM not in txt:
            return []
        return _parse_decisions(txt)
    except Exception:
        log.exception("Ошибка вызова Claude в extract_decisions")
        return []


def save_decisions(folder: Path, qa: list[tuple[str, str]]) -> None:
    """Решения руководителя — отдельным файлом РЕШЕНИЯ.md."""
    lines = ["# Решения руководителя\n"]
    for i, (q, ans) in enumerate(qa, start=1):
        lines.append(f"{i}. {q}\n   → {ans}\n")
    (folder / "РЕШЕНИЯ.md").write_text("\n".join(lines), encoding="utf-8")


# ──────────────────────────── Клавиатуры ────────────────────────────

def review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Показать подробно", callback_data="show_full")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit")],
        [InlineKeyboardButton(text="✅ Одобрить", callback_data="approve")],
        [InlineKeyboardButton(text="🔄 Переделать (другой угол)", callback_data="regenerate")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])


def _cat_btn(c: str, done: set[str]) -> InlineKeyboardButton:
    """Кнопка отдела в списке; ✅ если уже выполнен."""
    mark = "✅ " if c in done else ""
    return InlineKeyboardButton(
        text=f"{mark}{CAT_EMOJI.get(c, '📂')} {c}", callback_data=f"cat:{c}"
    )


def _nav_row() -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text="🆕 Новая идея", callback_data="restart"),
        InlineKeyboardButton(text="📂 Проекты", callback_data="projects"),
    ]


def categories_keyboard(cats: dict[str, str], done: set[str] | None = None) -> InlineKeyboardMarkup:
    """По кнопке на каждый отдел (✅ — выполненные) + навигация."""
    done = done or set()
    rows = [[_cat_btn(name, done)] for name in cats]
    rows.append(_nav_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_keyboard(
    name: str, cats: dict[str, str], done: set[str] | None = None
) -> InlineKeyboardMarkup:
    """Карточка отдела: Выполнить / (если уже выполнен) Переделать +
    переход к другим отделам."""
    done = done or set()
    rows = []
    if name in EXEC_ROLES:
        if name in done:
            rows.append([InlineKeyboardButton(
                text=f"🔄 Переделать «{name}»"[:60], callback_data=f"redo:{name}"
            )])
            rows.append([InlineKeyboardButton(
                text="✏️ Переделать с замечанием", callback_data=f"redofix:{name}"
            )])
        else:
            rows.append([InlineKeyboardButton(
                text=f"▶️ Выполнить «{name}»"[:60], callback_data=f"exec:{name}"
            )])
    rows += [[_cat_btn(c, done)] for c in cats if c != name]
    rows.append(_nav_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def result_keyboard(
    name: str, cats: dict[str, str], done: set[str] | None = None
) -> InlineKeyboardMarkup:
    """Под результатом отдела: переделать / с замечанием + переход."""
    done = done or set()
    rows = [
        [InlineKeyboardButton(text="🔄 Переделать", callback_data=f"redo:{name}")],
        [InlineKeyboardButton(
            text="✏️ Переделать с замечанием", callback_data=f"redofix:{name}"
        )],
    ]
    rows += [[_cat_btn(c, done)] for c in cats if c != name]
    rows.append(_nav_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def projects_keyboard(folders: list) -> InlineKeyboardMarkup:
    """Список сохранённых проектов кнопками (callback по индексу — лимит 64Б)."""
    rows = []
    for i, f in enumerate(folders):
        # имя папки: YYYYMMDD_HHMM_slug → дата + читаемое имя
        stem = f.name
        label = stem[9:13] + " " + stem[14:].replace("_", " ") if len(stem) > 14 else stem
        rows.append([InlineKeyboardButton(text=f"📁 {label}"[:60], callback_data=f"proj:{i}")])
    rows.append([InlineKeyboardButton(text="🆕 Новая идея", callback_data="restart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def decision_keyboard(idx: int, opts: list[str]) -> InlineKeyboardMarkup:
    """Кнопка на каждый вариант развилки + «решу позже»."""
    rows = [
        [InlineKeyboardButton(text=o[:60], callback_data=f"dec:{idx}:{j}")]
        for j, o in enumerate(opts)
    ]
    rows.append([
        InlineKeyboardButton(text="❓ Объясни", callback_data=f"dec:{idx}:explain"),
        InlineKeyboardButton(text="🤖 Реши сам", callback_data=f"dec:{idx}:auto"),
    ])
    rows.append([InlineKeyboardButton(
        text="⏭ Решу позже", callback_data=f"dec:{idx}:skip"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


TG_LIMIT = 4000  # лимит Telegram 4096, берём с запасом


async def send_long(message: Message, text: str) -> None:
    """Шлёт текст, разбивая на части по строкам, если длиннее лимита Telegram."""
    if len(text) <= TG_LIMIT:
        await message.answer(text)
        return
    chunk = ""
    for line in text.split("\n"):
        while len(line) > TG_LIMIT:  # очень длинная строка без переносов
            await message.answer(line[:TG_LIMIT])
            line = line[TG_LIMIT:]
        if len(chunk) + len(line) + 1 > TG_LIMIT:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk)


async def show_categories_summary(message: Message, state: FSMContext) -> None:
    """Финальная сводка: проект разнесён по отделам. Ставит Flow.categorized."""
    data = await state.get_data()
    cats: dict = data.get("cats", {})
    folder = Path(data.get("project_dir", ""))
    done = executed_set(folder)
    name = _slug(data.get("idea", "")).replace("_", " ")
    lines = "\n".join(
        f"{'✅ ' if c in done else ''}{CAT_EMOJI.get(c, '📂')} {c}" for c in cats
    )
    decided = "📌 <b>Решения:</b> РЕШЕНИЯ.md\n" if data.get("dec_answers") else ""
    await state.set_state(Flow.categorized)
    await message.answer(
        "✅ <b>План одобрен и разнесён по отделам</b>\n\n"
        f"📦 <b>Проект:</b> {escape(name)}\n"
        f"💾 <b>Сохранено:</b> <code>projects\\{escape(folder.name)}</code>\n"
        f"{decided}\n"
        f"🗂 <b>Отделы ({len(cats)}):</b> (✅ — выполнен)\n{lines}\n\n"
        "Тапни отдел — покажу его ТЗ. Новая идея — /new",
        reply_markup=categories_keyboard(cats, done),
        parse_mode="HTML",
    )


async def ask_next_decision(message: Message, state: FSMContext) -> bool:
    """Задаёт следующую развилку. True — задал, False — все пройдены."""
    data = await state.get_data()
    decisions: list = data.get("decisions", [])
    idx: int = data.get("dec_idx", 0)
    if idx >= len(decisions):
        return False
    d = decisions[idx]
    await message.answer(
        f"💡 <b>Решение {idx + 1}/{len(decisions)}</b>\n\n{escape(d['q'])}",
        reply_markup=decision_keyboard(idx, d["opts"]),
        parse_mode="HTML",
    )
    return True


async def finalize_categorization(message: Message, state: FSMContext) -> None:
    """Разносит план по отделам С УЧЁТОМ принятых решений, сохраняет проект,
    показывает сводку. Вызывается после развилок (или сразу, если их нет)."""
    data = await state.get_data()
    plan = data.get("full_plan", "")
    idea = data.get("idea", "")
    qa = [tuple(x) for x in data.get("dec_answers", [])]
    working = await message.answer("🗂 Разношу по отделам… ⏳")
    cats = await categorize_plan(plan, qa or None)
    await working.delete()
    if "__error__" in cats:
        await message.answer(cats["__error__"])
        return
    folder = save_project(idea, plan, cats)
    if qa:
        save_decisions(folder, qa)
    await state.update_data(cats=cats, project_dir=str(folder))
    await show_categories_summary(message, state)


# ──────────────────────────── Хендлеры ────────────────────────────

dp = Dispatcher(storage=MemoryStorage())


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    api_status = (
        "✅ Claude API подключён"
        if HAS_LLM
        else "⚠️ Claude API не подключён — работаю на заглушках"
    )
    await message.answer(
        "👋 <b>Привет! Я твой ассистент-фабрика проектов.</b>\n\n"
        "Кидаешь идею в любой форме → я делаю план → ты одобряешь → "
        "я разношу всё по отделам "
        "(🎨 дизайн, 💻 код, 🎵 музыка, 📝 наполнение, 💡 предложения) "
        "и складываю в папку проекта.\n\n"
        "<b>Команды:</b>\n"
        "🆕 /new — новая идея\n"
        "📂 /projects — мои проекты\n"
        "🛑 /cancel — отменить текущую\n\n"
        f"<b>Статус:</b> {api_status}\n\n"
        "Поехали — жми /new",
        parse_mode="HTML",
    )


@dp.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Flow.waiting_for_idea)
    await message.answer("Скинь идею в любой форме — текстом, как угодно. Жду.")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Окей, отменил. Когда будешь готов — /new.")


async def show_projects_list(message: Message, state: FSMContext) -> None:
    """Список сохранённых проектов кнопками. Сохраняет порядок в FSM."""
    folders = list_projects()
    if not folders:
        await message.answer("Пока нет сохранённых проектов. Начни — /new")
        return
    await state.update_data(proj_list=[str(f) for f in folders])
    await message.answer(
        f"📂 <b>Проекты ({len(folders)})</b>\nВыбери — открою его отделы:",
        reply_markup=projects_keyboard(folders),
        parse_mode="HTML",
    )


@dp.message(Command("projects"))
async def cmd_projects(message: Message, state: FSMContext) -> None:
    await show_projects_list(message, state)


@dp.message(Flow.waiting_for_idea, F.text)
async def receive_idea(message: Message, state: FSMContext) -> None:
    idea = message.text or ""
    thinking = await message.answer("Думаю… ⏳")
    overview, full = await generate_plan(idea)
    await state.update_data(idea=idea, overview=overview, full_plan=full)
    await state.set_state(Flow.reviewing_plan)
    await thinking.delete()
    await send_long(message, overview)
    await message.answer(
        "👆 Это краткий обзор. Что делаем?", reply_markup=review_keyboard()
    )


@dp.callback_query(Flow.reviewing_plan, F.data == "approve")
async def cb_approve(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Одобрено, анализирую…")
    data = await state.get_data()
    plan = data.get("full_plan", "")
    working = await callback.message.answer("🔎 Ищу развилки в плане… ⏳")
    decisions = await extract_decisions(plan)
    await working.delete()
    await state.update_data(decisions=decisions, dec_idx=0, dec_answers=[])

    if decisions:
        await state.set_state(Flow.deciding)
        await callback.message.answer(
            f"💡 <b>Сначала {len(decisions)} развилок(и) за тобой.</b>\n"
            "Ты руководитель — реши. Не уверен — «❓ Объясни» или «🤖 Реши сам». "
            "Решённое не попадёт в дубли ПРЕДЛОЖЕНИЙ и учтётся в ТЗ отделов.",
            parse_mode="HTML",
        )
        await ask_next_decision(callback.message, state)
    else:
        await finalize_categorization(callback.message, state)


@dp.callback_query(Flow.categorized, F.data.startswith("cat:"))
async def cb_category(callback: CallbackQuery, state: FSMContext) -> None:
    name = callback.data.split("cat:", 1)[1]
    await callback.answer(f"{name}…")
    data = await state.get_data()
    cats: dict = data.get("cats", {})
    body = cats.get(name)
    if not body:
        await callback.message.answer("Этот отдел пуст или не найден.")
        return
    emoji = CAT_EMOJI.get(name, "📂")
    await callback.message.answer(
        f"{emoji} <b>ОТДЕЛ: {escape(name)}</b>", parse_mode="HTML"
    )
    await send_long(callback.message, body)
    done = executed_set(Path(data.get("project_dir", "")))
    if name not in EXEC_ROLES:
        hint = "Этот отдел уже решён на развилках, исполнять нечего."
    elif name in done:
        hint = "✅ Этот отдел уже выполнен. Можно переделать (заново / с замечанием)."
    else:
        hint = "Жми «▶️ Выполнить» — прогоню это ТЗ через Claude."
    await callback.message.answer(
        hint, reply_markup=category_keyboard(name, cats, done)
    )


async def deliver_execution(
    message: Message, state: FSMContext, name: str, feedback: str = ""
) -> None:
    """Исполняет отдел, шлёт КОРОТКУЮ сводку + результат ФАЙЛОМ (код — zip),
    кнопки переделать."""
    data = await state.get_data()
    cats: dict = data.get("cats", {})
    task = cats.get(name)
    if not task or name not in EXEC_ROLES:
        await message.answer("Этот отдел нельзя исполнить.")
        return
    plan = data.get("full_plan", "")
    folder = Path(data.get("project_dir", ""))
    dec_text = ""
    dec_file = folder / "РЕШЕНИЯ.md"
    if dec_file.exists():
        dec_text = dec_file.read_text(encoding="utf-8")

    emoji = CAT_EMOJI.get(name, "📂")
    note = " (с учётом замечания)" if feedback else ""
    working = await message.answer(
        f"{emoji} Отдел «{escape(name)}» работает{escape(note)}… ⏳ (до минуты)",
        parse_mode="HTML",
    )
    result = await execute_department(name, task, plan, dec_text, feedback)
    summary = await summarize_result(name, result)
    await working.delete()

    base = name.replace(" ", "_")
    if folder.exists():
        (folder / f"{base}_РЕЗУЛЬТАТ.md").write_text(result, encoding="utf-8")

    # короткая сводка в чат
    await message.answer(
        f"{emoji} <b>ГОТОВО — отдел {escape(name)}</b>\n\n"
        f"{escape(summary)}\n\n"
        "📎 Полный результат — в файле ниже.",
        parse_mode="HTML",
    )

    # результат файлом
    sent_zip = False
    if name == "КОД":
        code_files, truncated = extract_code_files(result)
        if code_files:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for fn, content in code_files.items():
                    z.writestr(fn, content)
                z.writestr("_ПОЛНЫЙ_ОТВЕТ.md", result)
            file_list = ", ".join(list(code_files)[:6])
            cap = f"💻 Файлы: {file_list}. Распакуй и запусти."
            if truncated:
                cap += " ⚠️ Код длинный и оборвался — жми «🔄 Переделать»."
            await message.answer_document(
                BufferedInputFile(buf.getvalue(), filename=f"{base}_код.zip"),
                caption=cap[:1000],
            )
            sent_zip = True
    if not sent_zip:
        md_bytes = result.encode("utf-8")
        await message.answer_document(
            BufferedInputFile(md_bytes, filename=f"{base}_РЕЗУЛЬТАТ.md"),
            caption="📄 Полный результат отдела.",
        )

    await state.set_state(Flow.categorized)
    await message.answer(
        "Не то? Переделаю. Или к другому отделу:",
        reply_markup=result_keyboard(name, cats, executed_set(folder)),
    )


@dp.callback_query(Flow.categorized, F.data.startswith("exec:"))
async def cb_exec(callback: CallbackQuery, state: FSMContext) -> None:
    name = callback.data.split("exec:", 1)[1]
    await callback.answer(f"Выполняю «{name}»…")
    await deliver_execution(callback.message, state, name)


@dp.callback_query(Flow.categorized, F.data.startswith("redo:"))
async def cb_redo(callback: CallbackQuery, state: FSMContext) -> None:
    name = callback.data.split("redo:", 1)[1]
    await callback.answer(f"Переделываю «{name}»…")
    await deliver_execution(callback.message, state, name)


@dp.callback_query(Flow.categorized, F.data.startswith("redofix:"))
async def cb_redofix(callback: CallbackQuery, state: FSMContext) -> None:
    name = callback.data.split("redofix:", 1)[1]
    await callback.answer()
    await state.update_data(redo_name=name)
    await state.set_state(Flow.redo_feedback)
    await callback.message.answer(
        f"✏️ Что не так с результатом отдела «{escape(name)}»? "
        "Напиши конкретно что переделать — учту и прогоню заново.",
        parse_mode="HTML",
    )


@dp.message(Flow.redo_feedback, F.text)
async def receive_redo_feedback(message: Message, state: FSMContext) -> None:
    feedback = message.text or ""
    data = await state.get_data()
    name = data.get("redo_name", "")
    await state.set_state(Flow.categorized)
    if not name:
        await message.answer("Не понял какой отдел. Открой отдел заново.")
        return
    await deliver_execution(message, state, name, feedback=feedback)


@dp.callback_query(F.data == "restart")
async def cb_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(Flow.waiting_for_idea)
    await callback.message.answer("Скинь новую идею в любой форме. Жду.")


@dp.callback_query(F.data == "projects")
async def cb_projects(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await show_projects_list(callback.message, state)


@dp.callback_query(F.data.startswith("proj:"))
async def cb_open_project(callback: CallbackQuery, state: FSMContext) -> None:
    i = int(callback.data.split("proj:", 1)[1])
    data = await state.get_data()
    names = data.get("proj_list") or [str(p) for p in list_projects()]
    if i >= len(names):
        await callback.answer("Проект не найден")
        return
    folder = Path(names[i])
    await callback.answer("Открываю…")
    proj = load_project(folder)
    await state.clear()
    await state.update_data(
        idea=proj["idea"], full_plan=proj["full_plan"],
        cats=proj["cats"], project_dir=str(folder),
    )
    if not proj["cats"]:
        await state.set_state(Flow.waiting_for_idea)
        await callback.message.answer(
            "В этом проекте нет файлов отделов. /new — начать новую идею."
        )
        return
    await show_categories_summary(callback.message, state)


@dp.callback_query(Flow.deciding, F.data.startswith("dec:"))
async def cb_decision(callback: CallbackQuery, state: FSMContext) -> None:
    _, idx_s, opt_s = callback.data.split(":", 2)
    idx = int(idx_s)
    data = await state.get_data()
    decisions: list = data.get("decisions", [])
    cur: int = data.get("dec_idx", 0)
    if idx != cur or idx >= len(decisions):
        await callback.answer("Уже отвечено")
        return
    d = decisions[idx]
    plan = data.get("full_plan", "")

    if opt_s == "explain":
        await callback.answer("Объясняю…")
        txt = await explain_options(d["q"], d["opts"], plan)
        await callback.message.answer(
            f"❓ <b>{escape(d['q'])}</b>", parse_mode="HTML"
        )
        await send_long(callback.message, txt)
        await ask_next_decision(callback.message, state)  # тот же вопрос снова
        return

    if opt_s == "auto":
        await callback.answer("Бот выбирает…")
        j = await auto_pick(d["q"], d["opts"], plan)
        chosen = f"{d['opts'][j]} (🤖 бот выбрал)"
    elif opt_s == "skip":
        await callback.answer("Отложено")
        chosen = "(решу позже)"
    else:
        j = int(opt_s)
        chosen = d["opts"][j] if 0 <= j < len(d["opts"]) else "(?)"
        await callback.answer(chosen[:60])

    answers: list = data.get("dec_answers", [])
    answers.append([d["q"], chosen])
    await state.update_data(dec_idx=cur + 1, dec_answers=answers)
    await callback.message.answer(
        f"✅ {escape(d['q'])}\n→ <b>{escape(chosen)}</b>", parse_mode="HTML"
    )

    if not await ask_next_decision(callback.message, state):
        await callback.message.answer(
            "📌 Решения приняты. Разношу по отделам с их учётом…"
        )
        await finalize_categorization(callback.message, state)


@dp.callback_query(Flow.reviewing_plan, F.data == "show_full")
async def cb_show_full(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Разворачиваю полный план…")
    data = await state.get_data()
    full = data.get("full_plan", "")
    await send_long(callback.message, full or "Полный план не найден, переделай идею.")
    await callback.message.answer(
        "👆 Полный план. Что делаем?", reply_markup=review_keyboard()
    )


@dp.callback_query(Flow.reviewing_plan, F.data == "regenerate")
async def cb_regenerate(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Делаю другой вариант…")
    data = await state.get_data()
    idea = data.get("idea", "")
    previous = data.get("full_plan", "")
    overview, full = await generate_plan(idea, previous_attempt=previous)
    await state.update_data(overview=overview, full_plan=full)
    await send_long(callback.message, overview)
    await callback.message.answer(
        "👆 Новый вариант (краткий обзор). Что делаем?",
        reply_markup=review_keyboard(),
    )


@dp.callback_query(Flow.reviewing_plan, F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await state.clear()
    await callback.message.answer("Окей, бросил эту идею. /new — начать новую.")


@dp.callback_query(Flow.reviewing_plan, F.data == "edit")
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(Flow.editing_plan)
    await callback.message.answer(
        "✏️ <b>Что поправить?</b> Напиши номер пункта/подпункта и что сделать.\n\n"
        "Примеры:\n"
        "• <i>убери пункт 3.2</i>\n"
        "• <i>в 1.1 замени мобильное приложение на Telegram-бота</i>\n"
        "• <i>добавь пункт про тестирование после реализации</i>\n\n"
        "Можно несколько правок сразу, одним сообщением.",
        parse_mode="HTML",
    )


@dp.callback_query()
async def cb_stale(callback: CallbackQuery) -> None:
    """Любая кнопка, не подошедшая под текущее состояние (напр. после
    перезапуска бота — состояния в памяти теряются)."""
    await callback.answer("Кнопка устарела (бот перезапускался). Жми /new или /projects.", show_alert=True)


@dp.message(Flow.editing_plan, F.text)
async def receive_edit(message: Message, state: FSMContext) -> None:
    edit_request = message.text or ""
    data = await state.get_data()
    full = data.get("full_plan", "")
    thinking = await message.answer("Вношу правки… ⏳")
    overview, new_full = await edit_plan(full, edit_request)
    await state.update_data(overview=overview, full_plan=new_full)
    await state.set_state(Flow.reviewing_plan)
    await thinking.delete()
    await send_long(message, overview)
    await message.answer(
        "👆 Обновлённый план (краткий обзор). Что делаем?",
        reply_markup=review_keyboard(),
    )


@dp.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer("Жду команду. Попробуй /new чтобы начать новую идею.")


# ──────────────────────────── Запуск ────────────────────────────

async def main() -> None:
    if PROXY_URL:
        session = AiohttpSession(proxy=PROXY_URL)
        bot = Bot(token=BOT_TOKEN, session=session)
        log.info(f"Бот запускается через прокси {PROXY_URL}…")
    else:
        bot = Bot(token=BOT_TOKEN)
        log.info("Бот запускается напрямую (без прокси)…")
    log.info("(нажми Ctrl+C чтобы остановить)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
