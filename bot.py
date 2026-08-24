import os
import asyncio
import logging
import aiosqlite
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Loglarni sozlash
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_OWNER_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
DB_NAME = "bot_data.db"

# --- СОСТОЯНИЯ FSM ---
class GroupSettingsState(StatesGroup):
    waiting_for_limit = State()
    waiting_for_days = State()
    waiting_for_channel = State()
    waiting_for_delete_delay = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                required_limit INTEGER DEFAULT 3,
                duration_days INTEGER DEFAULT 7,
                channel TEXT DEFAULT '',
                warning_delete_delay INTEGER DEFAULT 5
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
                chat_id INTEGER PRIMARY KEY,
                title TEXT DEFAULT '',
                added_by INTEGER DEFAULT 0
            )
        """)
        await db.commit()

        try:
            await db.execute("ALTER TABLE registered_groups ADD COLUMN title TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE registered_groups ADD COLUMN added_by INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE group_settings ADD COLUMN warning_delete_delay INTEGER DEFAULT 5")
        except Exception:
            pass
        await db.commit()

async def get_group_settings(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT required_limit, duration_days, channel, warning_delete_delay FROM group_settings WHERE chat_id=?", 
            (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"limit": row[0], "days": row[1], "channel": row[2], "delete_delay": row[3] if len(row) > 3 and row[3] is not None else 5}
            else:
                await db.execute(
                    "INSERT INTO group_settings (chat_id, required_limit, duration_days, channel, warning_delete_delay) VALUES (?, 3, 7, '', 5)",
                    (chat_id,)
                )
                await db.commit()
                return {"limit": 3, "days": 7, "channel": "", "delete_delay": 5}

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

# --- ХЕНДЛЕР `/start` ---
@dp.message(Command("start"))
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    
    if message.chat.type in ["group", "supergroup"]:
        await message.reply("🤖 Бот активен в группе! Для просмотра настроек отправьте команду /panel.")
        return

    me = await bot.get_me()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="⚙️ Управление группами", callback_data="my_groups")],
        [types.InlineKeyboardButton(text="➕ Добавить бота в группу", url=f"https://t.me/{me.username}?startgroup=true")]
    ])

    if message.from_user.id == BOT_OWNER_ID:
        kb.inline_keyboard.append([types.InlineKeyboardButton(text="👑 Панель главного админа", callback_data="owner_admin")])

    user_name = message.from_user.full_name.replace("<", "&lt;").replace(">", "&gt;")

    await message.reply(
        f"👋 <b>Добро пожаловать, {user_name}!</b>\n\n"
        f"С помощью этого бота вы можете настроить <b>обязательное добавление участников</b> и <b>обязательную подписку на канал</b> в ваших группах.\n\n"
        f"Чтобы начать, добавьте бота в свою группу и выдайте ему права <b>Администратора</b>!",
        reply_markup=kb,
        parse_mode="HTML"
    )

# --- ПАНЕЛЬ ГЛАВНОГО АДМИНА ---
@dp.message(Command("admin"), F.from_user.id == BOT_OWNER_ID)
@dp.callback_query(F.data == "owner_admin", F.from_user.id == BOT_OWNER_ID)
async def owner_panel(event: types.Message | types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM registered_groups") as cursor:
            groups_count = (await cursor.fetchone())[0]
        
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM group_users") as cursor:
            users_count = (await cursor.fetchone())[0]

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📢 Рассылка по группам", callback_data="start_broadcast")],
        [types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_to_start")]
    ])

    text = (
        f"👑 <b>Панель главного админа</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Количество групп: <b>{groups_count}</b>\n"
        f"• Уникальных пользователей: <b>{users_count}</b>"
    )

    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.reply(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "start_broadcast", F.from_user.id == BOT_OWNER_ID)
async def prompt_broadcast(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Введите сообщение для рассылки по всем группам:")
    await state.set_state(BroadcastState.waiting_for_message)
    await call.answer()

@dp.message(BroadcastState.waiting_for_message, F.from_user.id == BOT_OWNER_ID)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    status_msg = await message.answer("🔄 Идет отправка сообщений...")

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
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"• Успешно: <b>{success}</b> групп\n"
        f"• Ошибок: <b>{failed}</b>",
        parse_mode="HTML"
    )

# --- МЕНЮ ГРУПП ПОЛЬЗОВАТЕЛЯ ---
@dp.callback_query(F.data == "my_groups")
@dp.callback_query(F.data == "back_to_start")
async def show_user_groups(call: types.CallbackQuery):
    if call.data == "back_to_start":
        me = await bot.get_me()
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⚙️ Управление группами", callback_data="my_groups")],
            [types.InlineKeyboardButton(text="➕ Добавить бота в группу", url=f"https://t.me/{me.username}?startgroup=true")]
        ])
        if call.from_user.id == BOT_OWNER_ID:
            kb.inline_keyboard.append([types.InlineKeyboardButton(text="👑 Панель главного админа", callback_data="owner_admin")])

        user_name = call.from_user.full_name.replace("<", "&lt;").replace(">", "&gt;")

        await call.message.edit_text(
            f"👋 <b>Добро пожаловать, {user_name}!</b>\n\n"
            f"С помощью этого бота вы можете настроить <b>обязательное добавление участников</b> и <b>обязательную подписку на канал</b> в ваших группах.\n\n"
            f"Чтобы начать, добавьте бота в свою группу и выдайте ему права <b>Администратора</b>!",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await call.answer()
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id, title FROM registered_groups WHERE added_by=?", (call.from_user.id,)) as cursor:
            groups = await cursor.fetchall()

    buttons = []
    for g_id, g_title in groups:
        name = g_title if g_title else f"Группа ({g_id})"
        buttons.append([types.InlineKeyboardButton(text=f"👥 {name}", callback_data=f"manage_g:{g_id}")])

    buttons.append([types.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")])
    kb = types.InlineKeyboardMarkup(inline_keyboard=buttons)

    if not groups:
        await call.message.edit_text(
            "❌ <b>Вы еще не подключили бота ни к одной группе!</b>\n\n"
            "Добавьте бота в группу, выдайте права администратора или отправьте <code>/panel</code> в группе, чтобы она появилась здесь.",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        await call.message.edit_text("📋 <b>Выберите группу для настройки:</b>", reply_markup=kb, parse_mode="HTML")
    await call.answer()

# --- НАСТРОЙКИ ГРУППЫ ---
@dp.callback_query(F.data.startswith("manage_g:"))
async def manage_group_menu(call: types.CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    settings = await get_group_settings(chat_id)
    
    channel_display = settings['channel'] if settings['channel'] else "❌ Отключен"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="👥 Изменить лимит инвайтов", callback_data=f"set_limit:{chat_id}")],
        [types.InlineKeyboardButton(text="⏳ Изменить срок доступа (дней)", callback_data=f"set_days:{chat_id}")],
        [types.InlineKeyboardButton(text="📢 Изменить обязательный канал", callback_data=f"set_chan:{chat_id}")],
        [types.InlineKeyboardButton(text="⏱ Изменить время удаления предупреждений", callback_data=f"set_delay:{chat_id}")],
        [types.InlineKeyboardButton(text="⬅️ Вернуться к списку групп", callback_data="my_groups")]
    ])

    await call.message.edit_text(
        f"⚙️ <b>Настройки группы</b>\n\n"
        f"• <b>Лимит добавления участников:</b> {settings['limit']} чел.\n"
        f"• <b>Срок разрешения на отправку сообщений:</b> {settings['days']} дней\n"
        f"• <b>Обязательный канал:</b> {channel_display}\n"
        f"• <b>Удаление предупреждения через:</b> {settings['delete_delay']} сек.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("set_limit:"))
async def process_limit_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_limit)
    await call.message.answer("✏️ <b>Сколько человек должен добавить пользователь?</b> (Отправьте число, например: 5):", parse_mode="HTML")
    await call.answer()

@dp.message(GroupSettingsState.waiting_for_limit)
async def save_limit(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("⚠️ Пожалуйста, введите только число!")
    
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE group_settings SET required_limit=? WHERE chat_id=?", (int(message.text), chat_id))
        await db.commit()
        
    await state.clear()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚙️ Вернуться к настройкам", callback_data=f"manage_g:{chat_id}")]])
    await message.answer(f"✅ Лимит добавления участников установлен на <b>{message.text} чел.</b>!", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("set_days:"))
async def process_days_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_days)
    await call.message.answer("✏️ <b>На сколько дней разрешать писать после добавления людей?</b> (Отправьте число, например: 7):", parse_mode="HTML")
    await call.answer()

@dp.message(GroupSettingsState.waiting_for_days)
async def save_days(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("⚠️ Пожалуйста, введите только число!")
    
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE group_settings SET duration_days=? WHERE chat_id=?", (int(message.text), chat_id))
        await db.commit()
        
    await state.clear()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚙️ Вернуться к настройкам", callback_data=f"manage_g:{chat_id}")]])
    await message.answer(f"✅ Срок доступа установлен на <b>{message.text} дней</b>!", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("set_chan:"))
async def process_chan_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_channel)
    await call.message.answer("✏️ <b>Отправьте username обязательного канала</b> (Например: <code>@username_kanala</code> или отправьте <code>0</code> для отключения):", parse_mode="HTML")
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
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚙️ Вернуться к настройкам", callback_data=f"manage_g:{chat_id}")]])
    await message.answer(f"✅ Обязательный канал установлен: <b>{chan if chan else 'Отключен'}</b>!", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("set_delay:"))
async def process_delay_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_delete_delay)
    await call.message.answer("✏️ <b>Через сколько секунд удалять предупреждения?</b> (Отправьте число в секундах, например: 5):", parse_mode="HTML")
    await call.answer()

@dp.message(GroupSettingsState.waiting_for_delete_delay)
async def save_delay(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("⚠️ Пожалуйста, введите только число!")
    
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    delay = int(message.text)
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE group_settings SET warning_delete_delay=? WHERE chat_id=?", (delay, chat_id))
        await db.commit()
        
    await state.clear()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚙️ Вернуться к настройкам", callback_data=f"manage_g:{chat_id}")]])
    await message.answer(f"✅ Время удаления предупреждений установлено на <b>{delay} сек.</b>!", reply_markup=kb, parse_mode="HTML")

# --- СОБЫТИЯ В ГРУППЕ ---
@dp.my_chat_member()
async def bot_added_to_group(event: types.ChatMemberUpdated):
    if event.chat.type in ["group", "supergroup"] and event.new_chat_member.status in ["administrator", "member"]:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT OR REPLACE INTO registered_groups (chat_id, title, added_by) VALUES (?, ?, ?)",
                (event.chat.id, event.chat.title, event.from_user.id)
            )
            await db.commit()
        await get_group_settings(event.chat.id)

@dp.message(Command("panel"), F.chat.type.in_({"group", "supergroup"}))
async def open_group_panel_chat(message: types.Message):
    if not await is_group_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("⚠️ Эта команда доступна только администраторам группы!")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO registered_groups (chat_id, title, added_by) VALUES (?, ?, ?)",
            (message.chat.id, message.chat.title, message.from_user.id)
        )
        await db.commit()

    me = await bot.get_me()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="⚙️ Открыть настройки в ЛС", url=f"https://t.me/{me.username}?start=my_groups")]
    ])
    await message.reply("⚙️ Для настройки группы перейдите в личные сообщения бота по кнопке ниже:", reply_markup=kb)

# Guruhi tark etgan a'zolar xabarini o'chirish
@dp.message(F.left_chat_member)
async def delete_left_chat_member_message(message: types.Message):
    try:
        await message.delete()
    except Exception:
        pass

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
                    user_name = inviter.full_name.replace("<", "&lt;").replace(">", "&gt;")
                    await message.answer(
                        f"🎉 {user_name}, вы выполнили условие ({settings['limit']} чел.)! Доступ предоставлен на <b>{settings['days']} дней</b>.",
                        parse_mode="HTML"
                    )
                else:
                    await db.execute(
                        "INSERT OR REPLACE INTO group_users (chat_id, user_id, invites_count, expires_at) VALUES (?, ?, ?, NULL)",
                        (chat_id, inviter.id, new_count)
                    )
                await db.commit()

    # Yangi a'zo qo'shilganligi haqidagi Telegram xabarini o'chirish
    try:
        await message.delete()
    except Exception:
        pass

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def check_permissions(message: types.Message):
    chat_id = message.chat.id

    # 1. Kanal nomidan yozilgan xabarlarni o'tkazib yuborish
    if message.sender_chat:
        return

    # 2. Yashirin (Anonim) admin xabarlarini o'tkazib yuborish
    if message.from_user and message.from_user.id == 1087968824:
        return

    user_id = message.from_user.id

    # 3. Oddiy adminlarni tekshirish
    if await is_group_admin(bot, chat_id, user_id):
        return

    settings = await get_group_settings(chat_id)
    now = datetime.now()
    user_name = message.from_user.full_name.replace("<", "&lt;").replace(">", "&gt;")

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
            f"⚠️ <b>{user_name}</b>, чтобы писать в группу, сначала подпишитесь на обязательный канал!", reply_markup=kb, parse_mode="HTML"
        )
        await asyncio.sleep(settings['delete_delay'])
        await warning.delete()
        return

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
            f"⚠️ <b>{user_name}</b>, чтобы писать в чат, вам нужно добавить еще <b>{remaining}</b> чел.! (Доступ дается на {settings['days']} дней)",
            parse_mode="HTML"
        )
        await asyncio.sleep(settings['delete_delay'])
        await warning.delete()

# --- ВЕБ-СЕРВЕР И ПАРАЛЛЕЛЬНЫЙ ЗАПУСК ---
async def handle(request):
    return web.Response(text="Bot is active!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Веб-сервер успешно запущен.")

async def main():
    await init_db()
    
    # Сброс прошлых обновлений и вебхуков
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Пауза для предотвращения конфликта сессий
    await asyncio.sleep(2)
    
    # Запуск сервера и бот-поллинга
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
