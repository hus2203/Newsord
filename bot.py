import asyncio
import logging
import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
DB_PATH = os.path.join(os.path.dirname(__file__), "wordbot.db")

router = Router()

# Заполняется в main(); используется, чтобы создавать FSM-состояние
# из фоновых задач (напоминаний), где нет входящего Message.
STORAGE: MemoryStorage | None = None


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ru TEXT NOT NULL,
            en TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            correct_streak INTEGER NOT NULL DEFAULT 0,
            mistakes INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS answers_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            word_id INTEGER NOT NULL,
            correct INTEGER NOT NULL,
            answered_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            reminders_enabled INTEGER NOT NULL DEFAULT 0,
            interval_minutes INTEGER NOT NULL DEFAULT 15,
            last_sent_at TEXT,
            last_word_id INTEGER
        )
        """
    )
    # На случай обновления бота на уже существующей базе — добьём недостающую колонку
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(words)").fetchall()}
    if "mistakes" not in existing_cols:
        conn.execute("ALTER TABLE words ADD COLUMN mistakes INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


@dataclass
class Word:
    id: int
    user_id: int
    ru: str
    en: str
    level: int
    correct_streak: int
    mistakes: int


def add_word(user_id: int, ru: str, en: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO words (user_id, ru, en) VALUES (?, ?, ?)",
        (user_id, ru.strip().lower(), en.strip().lower()),
    )
    conn.commit()
    conn.close()


def get_words(user_id: int) -> list[Word]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, user_id, ru, en, level, correct_streak, mistakes FROM words WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return [Word(*row) for row in rows]


def delete_word(user_id: int, word_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM words WHERE user_id = ? AND id = ?", (user_id, word_id)
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def update_progress(user_id: int, word_id: int, correct: bool) -> None:
    conn = get_conn()
    if correct:
        conn.execute(
            """UPDATE words
               SET correct_streak = correct_streak + 1,
                   level = MIN(level + 1, 5)
               WHERE id = ?""",
            (word_id,),
        )
    else:
        conn.execute(
            """UPDATE words
               SET correct_streak = 0,
                   level = MAX(level - 1, 0),
                   mistakes = mistakes + 1
               WHERE id = ?""",
            (word_id,),
        )
    conn.execute(
        "INSERT INTO answers_log (user_id, word_id, correct, answered_at) VALUES (?, ?, ?, ?)",
        (user_id, word_id, int(correct), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def set_reminders(user_id: int, chat_id: int, enabled: bool, interval_minutes: int | None = None) -> None:
    conn = get_conn()
    if interval_minutes is None:
        conn.execute(
            """INSERT INTO settings (user_id, chat_id, reminders_enabled)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   chat_id = excluded.chat_id,
                   reminders_enabled = excluded.reminders_enabled""",
            (user_id, chat_id, int(enabled)),
        )
    else:
        conn.execute(
            """INSERT INTO settings (user_id, chat_id, reminders_enabled, interval_minutes)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   chat_id = excluded.chat_id,
                   reminders_enabled = excluded.reminders_enabled,
                   interval_minutes = excluded.interval_minutes""",
            (user_id, chat_id, int(enabled), interval_minutes),
        )
    conn.commit()
    conn.close()


def get_all_reminder_users() -> list[tuple[int, int, int, str | None]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, chat_id, interval_minutes, last_sent_at FROM settings WHERE reminders_enabled = 1"
    ).fetchall()
    conn.close()
    return rows


def mark_reminder_sent(user_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE settings SET last_sent_at = ? WHERE user_id = ?",
        (datetime.utcnow().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def set_last_word(user_id: int, word_id: int) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO settings (user_id, chat_id, last_word_id)
           VALUES (?, 0, ?)
           ON CONFLICT(user_id) DO UPDATE SET last_word_id = excluded.last_word_id""",
        (user_id, word_id),
    )
    conn.commit()
    conn.close()


def get_last_word_id(user_id: int) -> int | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT last_word_id FROM settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def upsert_chat_id(user_id: int, chat_id: int) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO settings (user_id, chat_id)
           VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id""",
        (user_id, chat_id),
    )
    conn.commit()
    conn.close()


def get_chat_id(user_id: int) -> int | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT chat_id FROM settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_daily_stats(since_iso: str) -> list[tuple[int, int, int]]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT user_id, COUNT(*), SUM(correct)
           FROM answers_log
           WHERE answered_at >= ?
           GROUP BY user_id""",
        (since_iso,),
    ).fetchall()
    conn.close()
    return rows


def get_top_mistakes(user_id: int, limit: int = 5) -> list[Word]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, user_id, ru, en, level, correct_streak, mistakes
           FROM words WHERE user_id = ? AND mistakes > 0
           ORDER BY mistakes DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [Word(*row) for row in rows]


# --------------------------------------------------------------------------
# Middleware: запоминаем chat_id при любом обращении
# --------------------------------------------------------------------------

class TrackChatMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is not None and chat is not None:
            try:
                upsert_chat_id(user.id, chat.id)
            except Exception:
                logging.exception("Не удалось сохранить chat_id")
        return await handler(event, data)


# --------------------------------------------------------------------------
# FSM состояния
# --------------------------------------------------------------------------

class AddWord(StatesGroup):
    waiting_pair = State()


class Learn(StatesGroup):
    waiting_flashcard_answer = State()


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот для изучения английских слов 🇷🇺➡️🇬🇧\n\n"
        "Команды:\n"
        "/add — добавить слово\n"
        "/mywords — список твоих слов\n"
        "/del <id> — удалить слово\n"
        "/learn — начать тренировку (случайно рус→англ или англ→рус)\n"
        "/remind_on <минуты> — присылать слово через заданный интервал (по умолчанию 15)\n"
        "/remind_off — выключить напоминания\n"
        "/stats — отчёт за последние сутки\n"
        "/stop — прервать текущее действие"
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, остановились. Набери /learn или /add, когда будешь готов.")


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.set_state(AddWord.waiting_pair)
    await message.answer(
        "Отправь слово в формате:\nслово_на_русском - слово_на_английском\n\n"
        "Например: яблоко - apple"
    )


@router.message(AddWord.waiting_pair)
async def process_add_word(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    if "-" not in text:
        await message.answer(
            "Не понял формат 🙈 Пришли так: слово_на_русском - слово_на_английском"
        )
        return
    ru, en = text.split("-", 1)
    ru, en = ru.strip(), en.strip()
    if not ru or not en:
        await message.answer("Оба слова должны быть не пустыми. Попробуй ещё раз.")
        return
    add_word(message.from_user.id, ru, en)
    await state.clear()
    await message.answer(f"Добавил: {ru} — {en} ✅\nМожешь добавить ещё через /add или начать /learn")


@router.message(Command("mywords"))
async def cmd_mywords(message: Message) -> None:
    words = get_words(message.from_user.id)
    if not words:
        await message.answer("Пока нет слов. Добавь через /add")
        return
    lines = [f"{w.id}. {w.ru} — {w.en} (уровень {w.level}/5)" for w in words]
    await message.answer("Твои слова:\n" + "\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    stats = {uid: (total, correct) for uid, total, correct in get_daily_stats(since)}
    total, correct = stats.get(message.from_user.id, (0, 0))
    wrong = total - correct
    mistaken = get_top_mistakes(message.from_user.id, limit=5)

    text = f"📊 За последние 24 часа:\nВсего ответов: {total}\n✅ Правильно: {correct}\n❌ Ошибок: {wrong}"
    if mistaken:
        lines = [f"— {w.ru} — {w.en} (ошибок: {w.mistakes})" for w in mistaken]
        text += "\n\nЧаще всего путаешь:\n" + "\n".join(lines)
    await message.answer(text)


@router.message(Command("del"))
async def cmd_del(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используй так: /del <id>. Id смотри в /mywords")
        return
    word_id = int(parts[1])
    if delete_word(message.from_user.id, word_id):
        await message.answer("Удалено ✅")
    else:
        await message.answer("Не нашёл слово с таким id у тебя")


# --------------------------------------------------------------------------
# Тренировка
# --------------------------------------------------------------------------

def pick_word(words: list[Word], user_id: int) -> Word:
    # Не повторяем подряд то же слово, если есть из чего выбрать
    last_id = get_last_word_id(user_id)
    candidates = words
    if len(words) > 1 and last_id is not None:
        candidates = [w for w in words if w.id != last_id] or words

    # Слова с более низким уровнем и с историей ошибок встречаются чаще
    weights = [max((4 - w.level) + w.mistakes * 2, 1) for w in candidates]
    chosen = random.choices(candidates, weights=weights, k=1)[0]
    set_last_word(user_id, chosen.id)
    return chosen


def pick_direction() -> str:
    return random.choice(["ru2en", "en2ru"])


async def send_quiz(message: Message, state: FSMContext, words: list[Word], target: Word, direction: str) -> None:
    distractors = [w for w in words if w.id != target.id]
    random.shuffle(distractors)
    options = [target] + distractors[:3]
    random.shuffle(options)

    if direction == "ru2en":
        question = target.ru
        option_text = lambda w: w.en
    else:
        question = target.en
        option_text = lambda w: w.ru

    buttons = [
        [InlineKeyboardButton(text=option_text(w), callback_data=f"quiz:{target.id}:{w.id}")]
        for w in options
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"Как переводится «{question}»?", reply_markup=kb)


async def send_flashcard(message: Message, state: FSMContext, target: Word, direction: str) -> None:
    await state.set_state(Learn.waiting_flashcard_answer)
    if direction == "ru2en":
        question, expected = target.ru, target.en
        hint = "на английский"
    else:
        question, expected = target.en, target.ru
        hint = "на русский"
    await state.update_data(target_id=target.id, expected=expected)
    await message.answer(f"Переведи слово ({hint}): «{question}»\n(напиши ответ текстом)")


@router.message(Command("learn"))
async def cmd_learn(message: Message, state: FSMContext) -> None:
    words = get_words(message.from_user.id)
    if not words:
        await message.answer("Сначала добавь хотя бы одно слово через /add")
        return

    target = pick_word(words, message.from_user.id)
    direction = pick_direction()

    if len(words) >= 4 and random.random() < 0.7:
        await send_quiz(message, state, words, target, direction)
    else:
        await send_flashcard(message, state, target, direction)


@router.message(Command("remind_on"))
async def cmd_remind_on(message: Message) -> None:
    parts = (message.text or "").split()
    interval = 15
    if len(parts) == 2:
        if not parts[1].isdigit() or not (1 <= int(parts[1]) <= 1440):
            await message.answer("Интервал укажи числом минут от 1 до 1440, например: /remind_on 40")
            return
        interval = int(parts[1])

    set_reminders(message.from_user.id, message.chat.id, True, interval)
    await message.answer(
        f"Включил напоминания — буду присылать слово каждые {interval} мин ⏰\n"
        "Изменить интервал: /remind_on <минуты> (например /remind_on 1 или /remind_on 40)\n"
        "Выключить: /remind_off"
    )


@router.message(Command("remind_off"))
async def cmd_remind_off(message: Message) -> None:
    set_reminders(message.from_user.id, message.chat.id, False)
    await message.answer("Выключил напоминания.")


@router.callback_query(F.data.startswith("quiz:"))
async def process_quiz_answer(callback: CallbackQuery) -> None:
    _, target_id_str, chosen_id_str = callback.data.split(":")
    target_id, chosen_id = int(target_id_str), int(chosen_id_str)

    words = get_words(callback.from_user.id)
    target = next((w for w in words if w.id == target_id), None)
    if target is None:
        await callback.answer("Слово уже удалено")
        return

    correct = target_id == chosen_id
    update_progress(callback.from_user.id, target_id, correct)

    if correct:
        await callback.message.edit_text(f"✅ Верно! {target.ru} — {target.en}")
    else:
        await callback.message.edit_text(
            f"❌ Неверно. {target.ru} — {target.en}"
        )
    await callback.answer()
    await callback.message.answer("Ещё раз? /learn")


@router.message(Learn.waiting_flashcard_answer)
async def process_flashcard_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    target_id = data["target_id"]
    expected = data["expected"]
    user_answer = (message.text or "").strip().lower()

    correct = user_answer == expected
    update_progress(message.from_user.id, target_id, correct)
    await state.clear()

    if correct:
        await message.answer("✅ Верно!")
    else:
        await message.answer(f"❌ Неверно. Правильный ответ: {expected}")
    await message.answer("Ещё раз? /learn")


# --------------------------------------------------------------------------
# Периодические напоминания
# --------------------------------------------------------------------------

async def send_reminder(bot: Bot, user_id: int, chat_id: int) -> None:
    words = get_words(user_id)
    if not words:
        return
    target = pick_word(words, user_id)
    direction = pick_direction()

    if len(words) >= 4 and random.random() < 0.7:
        distractors = [w for w in words if w.id != target.id]
        random.shuffle(distractors)
        options = [target] + distractors[:3]
        random.shuffle(options)

        if direction == "ru2en":
            question = target.ru
            option_text = lambda w: w.en
        else:
            question = target.en
            option_text = lambda w: w.ru

        buttons = [
            [InlineKeyboardButton(text=option_text(w), callback_data=f"quiz:{target.id}:{w.id}")]
            for w in options
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await bot.send_message(chat_id, f"⏰ Как переводится «{question}»?", reply_markup=kb)
    else:
        if direction == "ru2en":
            question, expected = target.ru, target.en
            hint = "на английский"
        else:
            question, expected = target.en, target.ru
            hint = "на русский"

        if STORAGE is not None:
            key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
            state = FSMContext(storage=STORAGE, key=key)
            await state.set_state(Learn.waiting_flashcard_answer)
            await state.update_data(target_id=target.id, expected=expected)

        await bot.send_message(
            chat_id, f"⏰ Переведи слово ({hint}): «{question}»\n(напиши ответ текстом)"
        )


async def reminder_tick(bot: Bot) -> None:
    now = datetime.utcnow()
    for user_id, chat_id, interval_minutes, last_sent_at in get_all_reminder_users():
        due = True
        if last_sent_at:
            elapsed = now - datetime.fromisoformat(last_sent_at)
            due = elapsed >= timedelta(minutes=interval_minutes)
        if not due:
            continue
        try:
            await send_reminder(bot, user_id, chat_id)
            mark_reminder_sent(user_id)
        except Exception:
            logging.exception("Ошибка при отправке напоминания user_id=%s", user_id)


async def daily_report_tick(bot: Bot) -> None:
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    for user_id, total, correct_sum in get_daily_stats(since):
        correct = correct_sum or 0
        wrong = total - correct
        chat_id = get_chat_id(user_id)
        if chat_id is None or total == 0:
            continue

        text = f"📊 Отчёт за сутки:\nВсего ответов: {total}\n✅ Правильно: {correct}\n❌ Ошибок: {wrong}"
        mistaken = get_top_mistakes(user_id, limit=5)
        if mistaken:
            lines = [f"— {w.ru} — {w.en} (ошибок: {w.mistakes})" for w in mistaken]
            text += "\n\nЧаще всего путаешь:\n" + "\n".join(lines)

        try:
            await bot.send_message(chat_id, text)
        except Exception:
            logging.exception("Не удалось отправить отчёт user_id=%s", user_id)


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------

async def main() -> None:
    global STORAGE
    init_db()
    bot = Bot(token=BOT_TOKEN)
    STORAGE = MemoryStorage()
    dp = Dispatcher(storage=STORAGE)
    dp.update.outer_middleware(TrackChatMiddleware())
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(reminder_tick, IntervalTrigger(minutes=1), args=[bot])
    scheduler.add_job(daily_report_tick, CronTrigger(hour=0, minute=0), args=[bot])
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
