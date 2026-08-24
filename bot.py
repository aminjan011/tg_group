import os
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Render Environment Variables orqali olinadi
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_OWNER_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
DB_NAME = "bot_data.db"

class GroupSettingsState(StatesGroup):
    waiting_for_limit = State()
    waiting_for_days = State()
    waiting_for_channel = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- BAZA BILAN ISHLASH ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                required_limit INTEGER DEFAULT 3,
                duration_days INTEGER DEFAULT 7,
                channel TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_users (
                chat_id INTEGER,
                user_id INTEGER,
                invites_count INTEGER DEFAULT 0,
                expires_at TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS registered_groups (
                chat_id INTEGER PRIMARY KEY
            )
        """)
        await db.commit()

async def get_group_settings(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO registered_groups (chat_id) VALUES (?)", (chat_id,))
        await db.commit()

        async with db.execute(
            "SELECT required_limit, duration_days, channel FROM group_settings WHERE chat_id=?", 
            (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"limit": row[0], "days": row[1], "channel": row[2]}
            else:
                await db.execute(
                    "INSERT INTO group_settings (chat_id, required_limit, duration_days, channel) VALUES (?, 3, 7, '')",
                    (chat_id,)
                )
                await db.commit()
                return {"limit": 3, "days": 7, "channel": ""}

async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False

async def check_channel_sub(user_id: int, channel: str) -> bool:
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception:
        return False

# --- BOSH ADMIN (BOT YARATUVCHISI) PANELI (`/admin`) ---
@dp.message(Command("admin"), F.from_user.id == BOT_OWNER_ID)
async def owner_panel(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM registered_groups") as cursor:
            groups_count = (await cursor.fetchone())[0]
        
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM group_users") as cursor:
            users_count = (await cursor.fetchone())[0]

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📢 Рассылка по группам", callback_data="start_broadcast")]
    ])

    await message.reply(
        f"👑 **Панель владельца бота**\n\n"
        f"📊 **Статистика:**\n"
        f"• Количество групп: **{groups_count}**\n"
        f"• Уникальных пользователей: **{users_count}**",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "start_broadcast", F.from_user.id == BOT_OWNER_ID)
async def prompt_broadcast(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Отправьте сообщение для рассылки по всем группам:")
    await state.set_state(BroadcastState.waiting_for_message)
    await call.answer()

@dp.message(BroadcastState.waiting_for_message, F.from_user.id == BOT_OWNER_ID)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    status_msg = await message.answer("🔄 Начинается рассылка...")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id FROM registered_groups") as cursor:
            groups = await cursor.fetchall()

    success, failed = 0, 0
    for group in groups:
        try:
            await message.copy_to(chat_id=group[0])
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ **Рассылка завершена!**\n\n"
        f"• Успешно: **{success}** групп\n"
        f"• Ошибок: **{failed}**",
        parse_mode="Markdown"
    )

# --- GURUH ADMINLARI UCHUN PANEL (`/panel`) ---
@dp.message(Command("panel"), F.chat.type.in_({"group", "supergroup"}))
async def open_group_panel(message: types.Message):
    if not await is_group_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("⚠️ Эта команда доступна только администраторам группы!")

    settings = await get_group_settings(message.chat.id)
    channel_display = settings['channel'] if settings['channel'] else "Не настроен"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="👥 Лимит инвайтов", callback_data=f"set_limit:{message.chat.id}")],
        [types.InlineKeyboardButton(text="⏳ Срок доступа (дни)", callback_data=f"set_days:{message.chat.id}")],
        [types.InlineKeyboardButton(text="📢 Обязательный канал", callback_data=f"set_chan:{message.chat.id}")]
    ])

    await message.reply(
        f"⚙️ **Настройки этой группы**\n\n"
        f"• Требуется инвайтов: **{settings['limit']} чел.**\n"
        f"• Срок доступа: **{settings['days']} дней**\n"
        f"• Обязательный канал: **{channel_display}**",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("set_limit:"))
async def process_limit_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    if not await is_group_admin(bot, chat_id, call.from_user.id):
        return await call.answer("Вы не админ!", show_alert=True)
    
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_limit)
    await call.message.answer("Введите новое количество инвайтов:")
    await call.answer()

@dp.message(GroupSettingsState.waiting_for_limit)
async def save_limit(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Введите только число!")
    
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE group_settings SET required_limit=? WHERE chat_id=?", (int(message.text), chat_id))
        await db.commit()
        
    await state.clear()
    await message.answer(f"✅ Лимит установлен: **{message.text} чел.**", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("set_days:"))
async def process_days_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    if not await is_group_admin(bot, chat_id, call.from_user.id):
        return await call.answer("Вы не админ!", show_alert=True)
    
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_days)
    await call.message.answer("Введите количество дней доступа:")
    await call.answer()

@dp.message(GroupSettingsState.waiting_for_days)
async def save_days(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Введите только число!")
    
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE group_settings SET duration_days=? WHERE chat_id=?", (int(message.text), chat_id))
        await db.commit()
        
    await state.clear()
    await message.answer(f"✅ Срок доступа установлен: **{message.text} дней**", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("set_chan:"))
async def process_chan_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    if not await is_group_admin(bot, chat_id, call.from_user.id):
        return await call.answer("Вы не админ!", show_alert=True)
    
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_channel)
    await call.message.answer("Отправьте @username канала (или '0' чтобы отключить):")
    await call.answer()

@dp.message(GroupSettingsState.waiting_for_channel)
async def save_chan(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    chan = message.text.strip()
    if chan == "0":
        chan = ""
        
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE group_settings SET channel=? WHERE chat_id=?", (chan, chat_id))
        await db.commit()
        
    await state.clear()
    await message.answer(f"✅ Обязательный канал сохранен: **{chan if chan else 'Отключен'}**", parse_mode="Markdown")

# --- GURUHDAGI ASOSIY TEKSHIRUV MANTIG'I ---
@dp.message(F.new_chat_members)
async def track_invites(message: types.Message):
    chat_id = message.chat.id
    inviter = message.from_user
    settings = await get_group_settings(chat_id)
    
    for member in message.new_chat_members:
        if member.id != inviter.id:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(
                    "SELECT invites_count FROM group_users WHERE chat_id=? AND user_id=?", 
                    (chat_id, inviter.id)
                ) as cursor:
                    row = await cursor.fetchone()
                    current_count = row[0] if row else 0
                
                new_count = current_count + 1
                
                if new_count >= settings['limit']:
                    expires_at = (datetime.now() + timedelta(days=settings['days'])).isoformat()
                    await db.execute(
                        "INSERT OR REPLACE INTO group_users (chat_id, user_id, invites_count, expires_at) VALUES (?, ?, 0, ?)",
                        (chat_id, inviter.id, expires_at)
                    )
                    await message.answer(
                        f"🎉 {inviter.full_name}, вы выполнили условие ({settings['limit']} чел.)! Доступ открыт на **{settings['days']} дней**.",
                        parse_mode="Markdown"
                    )
                else:
                    await db.execute(
                        "INSERT OR REPLACE INTO group_users (chat_id, user_id, invites_count, expires_at) VALUES (?, ?, ?, NULL)",
                        (chat_id, inviter.id, new_count)
                    )
                await db.commit()

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def check_permissions(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if await is_group_admin(bot, chat_id, user_id):
        return

    settings = await get_group_settings(chat_id)
    now = datetime.now()

    # 1. Kanalga obunani tekshirish
    if not await check_channel_sub(user_id, settings['channel']):
        try:
            await message.delete()
        except Exception:
            pass
        kb = None
        if settings['channel'].startswith("@"):
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{settings['channel'][1:]}")]
            ])
        warning = await message.answer(
            f"⚠️ {message.from_user.full_name}, сначала подпишитесь на обязательный канал!", reply_markup=kb
        )
        await asyncio.sleep(5)
        await warning.delete()
        return

    # 2. Odam qo'shish va muddatni tekshirish
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT invites_count, expires_at FROM group_users WHERE chat_id=? AND user_id=?", 
            (chat_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()

    can_write = False
    invites_count = 0

    if row:
        invites_count, expires_at_str = row
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if now < expires_at:
                can_write = True
            else:
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        "UPDATE group_users SET expires_at=NULL, invites_count=0 WHERE chat_id=? AND user_id=?", 
                        (chat_id, user_id)
                    )
                    await db.commit()
                can_write = False

    if not can_write:
        try:
            await message.delete()
        except Exception:
            pass
            
        remaining = settings['limit'] - invites_count
        warning = await message.answer(
            f"⚠️ {message.from_user.full_name}, вам нужно добавить ещё **{remaining}** чел. (Доступ даётся на {settings['days']} дней)",
            parse_mode="Markdown"
        )
        await asyncio.sleep(5)
        await warning.delete()

# --- RENDER PORT XATOSI OLISHNING OLDINI OLUVCHI DUMMY WEB SERVER ---
async def handle(request):
    return web.Response(text="Bot is running live on Render!")

async def main():
    await init_db()

    # Web-serverni ishga tushirish (Render talabi bo'yicha)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # Telegram bot polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
