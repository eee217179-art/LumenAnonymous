import asyncio
import logging
import os
import secrets

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

DB_PATH = "lumen_anonymous.db"
BOT_USERNAME = ""

router = Router()


# ───────────────────────── Состояния ─────────────────────────
class Sending(StatesGroup):
    writing = State()  # человек пишет анонимно владельцу ссылки


class Replying(StatesGroup):
    writing = State()  # человек пишет ответ в диалоге


# ───────────────────────── База данных ─────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                code TEXT UNIQUE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                UNIQUE(owner_id, sender_id)
            );
            CREATE TABLE IF NOT EXISTS bans (
                owner_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                PRIMARY KEY (owner_id, sender_id)
            );
            """
        )
        await db.commit()


async def get_code(user_id: int, regenerate: bool = False) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT code FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row and not regenerate:
            return row[0]
        code = secrets.token_urlsafe(6)
        await db.execute(
            "INSERT INTO users (user_id, code) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET code = excluded.code",
            (user_id, code),
        )
        await db.commit()
        return code


async def get_owner_by_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE code = ?", (code,))
        row = await cur.fetchone()
        return row[0] if row else None


async def get_or_create_thread(owner_id: int, sender_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO threads (owner_id, sender_id) VALUES (?, ?)",
            (owner_id, sender_id),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT id FROM threads WHERE owner_id = ? AND sender_id = ?",
            (owner_id, sender_id),
        )
        row = await cur.fetchone()
        return row[0]


async def get_thread(thread_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT owner_id, sender_id FROM threads WHERE id = ?", (thread_id,)
        )
        return await cur.fetchone()


async def is_banned(owner_id: int, sender_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM bans WHERE owner_id = ? AND sender_id = ?",
            (owner_id, sender_id),
        )
        return await cur.fetchone() is not None


async def ban(owner_id: int, sender_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO bans (owner_id, sender_id) VALUES (?, ?)",
            (owner_id, sender_id),
        )
        await db.commit()


# ───────────────────────── Клавиатуры ─────────────────────────
def menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Моя ссылка", callback_data="mylink")
    kb.button(text="🔄 Новая ссылка", callback_data="newlink")
    kb.adjust(2)
    return kb.as_markup()


def owner_msg_kb(thread_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Ответить", callback_data=f"reply:{thread_id}")
    kb.button(text="🚫 Заблокировать", callback_data=f"ban:{thread_id}")
    kb.adjust(2)
    return kb.as_markup()


def sender_msg_kb(thread_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Ответить", callback_data=f"reply:{thread_id}")
    return kb.as_markup()


def link_text(code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={code}"


# ───────────────────────── Доставка сообщений ─────────────────────────
async def deliver(bot: Bot, msg: Message, to_chat: int, header: str, kb: InlineKeyboardMarkup) -> bool:
    """Копирует любое сообщение (текст, фото, видео, голосовое, кружок, стикер,
    файл и т.д.) получателю без указания автора."""
    try:
        await bot.send_message(to_chat, header)
        await bot.copy_message(
            chat_id=to_chat,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
            reply_markup=kb,
        )
        return True
    except TelegramForbiddenError:
        return False  # получатель заблокировал бота
    except TelegramBadRequest as e:
        logging.warning("Не удалось доставить: %s", e)
        return False  # например, у получателя закрыты голосовые/кружки


# ───────────────────────── Команды ─────────────────────────
@router.message(CommandStart(deep_link=True))
async def start_with_link(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    owner_id = await get_owner_by_code(command.args)
    if owner_id is None:
        await message.answer("🔮 Эта ссылка недействительна или была обновлена.")
        return
    if owner_id == message.from_user.id:
        await message.answer(
            "💜 Это твоя собственная ссылка. Отправь её друзьям, "
            "и они смогут писать тебе анонимно!",
            reply_markup=menu_kb(),
        )
        return
    if await is_banned(owner_id, message.from_user.id):
        await message.answer("🚫 Ты не можешь отправлять сообщения этому человеку.")
        return

    await state.set_state(Sending.writing)
    await state.update_data(owner_id=owner_id)
    await message.answer(
        "🟣 <b>LumenAnonymous</b>\n\n"
        "Напиши сообщение — оно уйдёт <b>анонимно</b>.\n"
        "Можно отправлять текст, фото, видео, голосовые, кружки, стикеры, файлы — что угодно 💜\n\n"
        "Отмена — /cancel"
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    code = await get_code(message.from_user.id)
    await message.answer(
        "💜 <b>LumenAnonymous</b> — анонимные сообщения\n\n"
        "Отправь свою ссылку друзьям или выложи в профиль — "
        "люди смогут писать тебе <b>анонимно</b>: текст, фото, видео, "
        "голосовые, кружки и всё остальное.\n\n"
        f"🔗 Твоя ссылка:\n<code>{link_text(code)}</code>\n\n"
        "Ты сможешь отвечать им, не раскрывая ни себя, ни их 🔮",
        reply_markup=menu_kb(),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🟣 Отменено. Нажми /start, чтобы открыть меню.")


@router.callback_query(F.data == "mylink")
async def cb_mylink(call: CallbackQuery):
    code = await get_code(call.from_user.id)
    await call.message.answer(f"🔗 Твоя ссылка:\n<code>{link_text(code)}</code>")
    await call.answer()


@router.callback_query(F.data == "newlink")
async def cb_newlink(call: CallbackQuery):
    code = await get_code(call.from_user.id, regenerate=True)
    await call.message.answer(
        "🔄 Ссылка обновлена. Старая больше не работает.\n\n"
        f"🔗 Новая ссылка:\n<code>{link_text(code)}</code>"
    )
    await call.answer()


# ───────────────────────── Ответ / блокировка ─────────────────────────
@router.callback_query(F.data.startswith("reply:"))
async def cb_reply(call: CallbackQuery, state: FSMContext):
    thread_id = int(call.data.split(":")[1])
    thread = await get_thread(thread_id)
    if not thread or call.from_user.id not in thread:
        await call.answer("Диалог не найден.", show_alert=True)
        return
    await state.set_state(Replying.writing)
    await state.update_data(thread_id=thread_id)
    await call.message.answer("💜 Напиши ответ (любой тип сообщения). Отмена — /cancel")
    await call.answer()


@router.callback_query(F.data.startswith("ban:"))
async def cb_ban(call: CallbackQuery):
    thread_id = int(call.data.split(":")[1])
    thread = await get_thread(thread_id)
    if not thread or call.from_user.id != thread[0]:
        await call.answer("Нельзя выполнить это действие.", show_alert=True)
        return
    await ban(thread[0], thread[1])
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("🚫 Отправитель заблокирован. Он больше не сможет тебе писать.")
    await call.answer()


# ───────────────────────── Приём сообщений ─────────────────────────
@router.message(Sending.writing)
async def on_anon_message(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    owner_id = data.get("owner_id")
    sender_id = message.from_user.id

    if await is_banned(owner_id, sender_id):
        await state.clear()
        await message.answer("🚫 Ты не можешь отправлять сообщения этому человеку.")
        return

    thread_id = await get_or_create_thread(owner_id, sender_id)
    ok = await deliver(
        bot,
        message,
        owner_id,
        "🟣 <b>Новое анонимное сообщение</b>",
        owner_msg_kb(thread_id),
    )
    if ok:
        await message.answer("✅ Отправлено анонимно! Можешь написать ещё или нажми /cancel.")
    else:
        await message.answer("⚠️ Не удалось доставить сообщение. Возможно, получатель ограничил такой тип контента.")


@router.message(Replying.writing)
async def on_reply_message(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    thread = await get_thread(data.get("thread_id", 0))
    await state.clear()
    if not thread:
        await message.answer("Диалог не найден.")
        return

    owner_id, sender_id = thread
    user_id = message.from_user.id

    if user_id == owner_id:
        target = sender_id
        header = "💜 <b>Тебе ответили на анонимное сообщение</b>"
        kb = sender_msg_kb(data["thread_id"])
    else:
        if await is_banned(owner_id, sender_id):
            await message.answer("🚫 Ты не можешь отправлять сообщения этому человеку.")
            return
        target = owner_id
        header = "🟣 <b>Анонимный ответ</b>"
        kb = owner_msg_kb(data["thread_id"])

    ok = await deliver(bot, message, target, header, kb)
    await message.answer("✅ Ответ отправлен!" if ok else "⚠️ Не удалось доставить ответ.")


@router.message()
async def fallback(message: Message):
    await message.answer(
        "🟣 Нажми /start, чтобы получить свою ссылку, "
        "или перейди по чьей-то ссылке, чтобы написать анонимно."
    )


# ───────────────────────── Запуск ─────────────────────────
async def main():
    global BOT_USERNAME
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажи BOT_TOKEN в файле .env")

    await init_db()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username

    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
