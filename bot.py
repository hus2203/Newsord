import asyncio
import logging
import os
import random
import sqlite3
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
DB_PATH = os.path.join(os.path.dirname(__file__), "wordbot.db")

router = Router()


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
            correct_streak INTEGER NOT NULL DEFAULT 0
        )
        """
    )
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
        "SELECT id, user_id, ru, en, level, correct_streak FROM words WHERE user_id = ?",
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


def update_progress(word_id: int, correct: bool) -> None:
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
                   level = MAX(level - 1, 0)
               WHERE id = ?""",
            (word_id,),
        )
    conn.commit()
    conn.close()


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
        "/learn — начать тренировку\n"
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

def pick_word(words: list[Word]) -> Word:
    # Слова с более низким уровнем встречаются чаще
    weights = [max(6 - w.level, 1) for w in words]
    return random.choices(words, weights=weights, k=1)[0]


async def send_quiz(message: Message, state: FSMContext, words: list[Word], target: Word) -> None:
    distractors = [w for w in words if w.id != target.id]
    random.shuffle(distractors)
    options = [target] + distractors[:3]
    random.shuffle(options)

    buttons = [
        [InlineKeyboardButton(text=w.en, callback_data=f"quiz:{target.id}:{w.id}")]
        for w in options
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"Как переводится «{target.ru}»?", reply_markup=kb)


async def send_flashcard(message: Message, state: FSMContext, target: Word) -> None:
    await state.set_state(Learn.waiting_flashcard_answer)
    await state.update_data(target_id=target.id, target_en=target.en)
    await message.answer(f"Переведи слово: «{target.ru}»\n(напиши ответ текстом)")


@router.message(Command("learn"))
async def cmd_learn(message: Message, state: FSMContext) -> None:
    words = get_words(message.from_user.id)
    if not words:
        await message.answer("Сначала добавь хотя бы одно слово через /add")
        return

    target = pick_word(words)

    if len(words) >= 4 and random.random() < 0.7:
        await send_quiz(message, state, words, target)
    else:
        await send_flashcard(message, state, target)


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
    update_progress(target_id, correct)

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
    target_en = data["target_en"]
    user_answer = (message.text or "").strip().lower()

    correct = user_answer == target_en
    update_progress(target_id, correct)
    await state.clear()

    if correct:
        await message.answer("✅ Верно!")
    else:
        await message.answer(f"❌ Неверно. Правильный ответ: {target_en}")
    await message.answer("Ещё раз? /learn")


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------

async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
