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

# --- FSM HOLATLARI ---
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
        await db.commit()

async def get_group_settings(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
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

# --- `/start` HANDLERI ---
@dp.message(Command("start"))
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    
    if message.chat.type in ["group", "supergroup"]:
        await message.reply("🤖 Bot guruhda faol! Sozlamalarni ko'rish uchun /panel buyrug'ini yuboring.")
        return

    me = await bot.get_me()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="⚙️ Guruhlarni boshqarish", callback_data="my_groups")],
        [types.InlineKeyboardButton(text="➕ Botni guruhga qo'shish", url=f"https://t.me/{me.username}?startgroup=true")]
    ])

    if message.from_user.id == BOT_OWNER_ID:
        kb.inline_keyboard.append([types.InlineKeyboardButton(text="👑 Bosh Admin Paneli", callback_data="owner_admin")])

    await message.reply(
        f"👋 **Xush kelibsiz, {message.from_user.first_name}!**\n\n"
        f"Ushbu bot orqali guruhlaringizda **odam qo'shish majburiyati** va **majburiy kanal obunasini** sozlashingiz mumkin.\n\n"
        f"Boshlash uchun botni guruhingizga qo'shing va **Admin** huquqini bering!",
        reply_markup=kb,
        parse_mode="Markdown"
    )

# --- BOSH ADMIN PANELI ---
@dp.message(Command("admin"), F.from_user.id == BOT_OWNER_ID)
@dp.callback_query(F.data == "owner_admin", F.from_user.id == BOT_OWNER_ID)
async def owner_panel(event: types.Message | types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM registered_groups") as cursor:
            groups_count = (await cursor.fetchone())[0]
        
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM group_users") as cursor:
            users_count = (await cursor.fetchone())[0]

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📢 Guruhlarga xabar yuborish", callback_data="start_broadcast")],
        [types.InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="back_to_start")]
    ])

    text = (
        f"👑 **Bosh Admin Paneli**\n\n"
        f"📊 **Statistika:**\n"
        f"• Guruhlar soni: **{groups_count}**\n"
        f"• Unikal foydalanuvchilar: **{users_count}**"
    )

    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await event.answer()
    else:
        await event.reply(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "start_broadcast", F.from_user.id == BOT_OWNER_ID)
async def prompt_broadcast(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Barcha guruhlarga yuboriladigan xabarni kiriting:")
    await state.set_state(BroadcastState.waiting_for_message)
    await call.answer()

@dp.message(BroadcastState.waiting_for_message, F.from_user.id == BOT_OWNER_ID)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    status_msg = await message.answer("🔄 Xabar yuborilmoqda...")

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
        f"✅ **Xabar yuborish yakunlandi!**\n\n"
        f"• Muvaffaqiyatli: **{success}** ta guruh\n"
        f"• Xatolik: **{failed}** ta",
        parse_mode="Markdown"
    )

# --- FOYDALANUVCHINING GURUHLARI MENYUSI ---
@dp.callback_query(F.data == "my_groups")
@dp.callback_query(F.data == "back_to_start")
async def show_user_groups(call: types.CallbackQuery):
    if call.data == "back_to_start":
        me = await bot.get_me()
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⚙️ Guruhlarni boshqarish", callback_data="my_groups")],
            [types.InlineKeyboardButton(text="➕ Botni guruhga qo'shish", url=f"https://t.me/{me.username}?startgroup=true")]
        ])
        if call.from_user.id == BOT_OWNER_ID:
            kb.inline_keyboard.append([types.InlineKeyboardButton(text="👑 Bosh Admin Paneli", callback_data="owner_admin")])

        await call.message.edit_text(
            f"👋 **Xush kelibsiz, {call.from_user.first_name}!**\n\n"
            f"Ushbu bot orqali guruhlaringizda **odam qo'shish majburiyati** va **majburiy kanal obunasini** sozlashingiz mumkin.\n\n"
            f"Boshlash uchun botni guruhingizga qo'shing va **Admin** huquqini bering!",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await call.answer()
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id, title FROM registered_groups WHERE added_by=?", (call.from_user.id,)) as cursor:
            groups = await cursor.fetchall()

    buttons = []
    for g_id, g_title in groups:
        name = g_title if g_title else f"Guruh ({g_id})"
        buttons.append([types.InlineKeyboardButton(text=f"👥 {name}", callback_data=f"manage_g:{g_id}")])

    buttons.append([types.InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_start")])
    kb = types.InlineKeyboardMarkup(inline_keyboard=buttons)

    if not groups:
        await call.message.edit_text(
            "❌ **Siz hali hech qaysi guruhga botni ulaganingiz yo'q!**\n\n"
            "Botni guruhingizga qo'shib, Admin huquqini bersangiz yoki guruhda `/panel` deb yozsangiz, bu yerda guruhingiz paydo bo'ladi.",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        await call.message.edit_text("📋 **Sozlash uchun guruhingizni tanlang:**", reply_markup=kb, parse_mode="Markdown")
    await call.answer()

# --- GURUH SOZLAMALARI ---
@dp.callback_query(F.data.startswith("manage_g:"))
async def manage_group_menu(call: types.CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    settings = await get_group_settings(chat_id)
    
    channel_display = settings['channel'] if settings['channel'] else "❌ O'chirilgan"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="👥 Odam qo'shish limitini o'zgartirish", callback_data=f"set_limit:{chat_id}")],
        [types.InlineKeyboardButton(text="⏳ Ruxsat muddatini o'zgartirish (kun)", callback_data=f"set_days:{chat_id}")],
        [types.InlineKeyboardButton(text="📢 Majburiy kanalni o'zgartirish", callback_data=f"set_chan:{chat_id}")],
        [types.InlineKeyboardButton(text="⬅️ Guruhlar ro'yxatiga qaytish", callback_data="my_groups")]
    ])

    await call.message.edit_text(
        f"⚙️ **Guruh Sozlamalari**\n\n"
        f"• **Majburiy odam qo'shish soni:** {settings['limit']} ta\n"
        f"• **Yozish ruxsati beriladigan muddat:** {settings['days']} kun\n"
        f"• **Majburiy kanal:** {channel_display}",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("set_limit:"))
async def process_limit_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_limit)
    await call.message.answer("✏️ **Foydalanuvchi guruhga nechta odam qo'shishi kerak?** (Raqam yuboring, masalan: 5):", parse_mode="Markdown")
    await call.answer()

@dp.message(GroupSettingsState.waiting_for_limit)
async def save_limit(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("⚠️ Iltimos, faqat raqam yozing!")
    
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE group_settings SET required_limit=? WHERE chat_id=?", (int(message.text), chat_id))
        await db.commit()
        
    await state.clear()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚙️ Sozlamalarga qaytish", callback_data=f"manage_g:{chat_id}")]])
    await message.answer(f"✅ Odam qo'shish limiti **{message.text} ta** qilib belgilandi!", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("set_days:"))
async def process_days_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_days)
    await call.message.answer("✏️ **Odam qo'shgandan so'ng necha kun yozishga ruxsat berilsin?** (Raqam yuboring, masalan: 7):", parse_mode="Markdown")
    await call.answer()

@dp.message(GroupSettingsState.waiting_for_days)
async def save_days(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("⚠️ Iltimos, faqat raqam yozing!")
    
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE group_settings SET duration_days=? WHERE chat_id=?", (int(message.text), chat_id))
        await db.commit()
        
    await state.clear()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚙️ Sozlamalarga qaytish", callback_data=f"manage_g:{chat_id}")]])
    await message.answer(f"✅ Ruxsat muddati **{message.text} kun** qilib belgilandi!", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("set_chan:"))
async def process_chan_btn(call: types.CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(GroupSettingsState.waiting_for_channel)
    await call.message.answer("✏️ **Majburiy kanal usernamesini yuboring** (Masalan: `@kanal_username` yoki o'chirish uchun `0` yuboring):", parse_mode="Markdown")
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
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚙️ Sozlamalarga qaytish", callback_data=f"manage_g:{chat_id}")]])
    await message.answer(f"✅ Majburiy kanal **{chan if chan else 'O-chirilgan'}** qilindi!", reply_markup=kb, parse_mode="Markdown")

# --- GURUH HODISALARI ---
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
        return await message.reply("⚠️ Bu buyruq faqat guruh adminlari uchun!")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO registered_groups (chat_id, title, added_by) VALUES (?, ?, ?)",
            (message.chat.id, message.chat.title, message.from_user.id)
        )
        await db.commit()

    me = await bot.get_me()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="⚙️ Sozlamalarni shaxsiyda ochish", url=f"https://t.me/{me.username}?start=my_groups")]
    ])
    await message.reply("⚙️ Guruhingizni sozlash uchun quyidagi tugma orqali botning shaxsiy chatiga o'ting:", reply_markup=kb)

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
                        f"🎉 {inviter.full_name}, siz shartni bajardingiz ({settings['limit']} ta odam)! Ruxsat **{settings['days']} kun**ga berildi.",
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

    if not await check_channel_sub(user_id, settings['channel']):
        try:
            await message.delete()
        except Exception:
            pass
        kb = None
        if settings['channel'].startswith("@"):
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📢 Obuna bo'lish", url=f"https://t.me/{settings['channel'][1:]}")]
            ])
        warning = await message.answer(
            f"⚠️ {message.from_user.full_name}, yozish uchun avval majburiy kanalga obuna bo'ling!", reply_markup=kb
        )
        await asyncio.sleep(5)
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
            f"⚠️ {message.from_user.full_name}, yozish uchun guruhga yana **{remaining}** ta odam qo'shishingiz kerak! (Ruxsat {settings['days']} kunga beriladi)",
            parse_mode="Markdown"
        )
        await asyncio.sleep(5)
        await warning.delete()

# --- WEB SERVER VA PARALLEL ISHGA TUSHIRISH ---
async def handle(request):
    return web.Response(text="Bot is active!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_head("/", handle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Web server muvaffaqiyatli ishga tushdi.")

async def main():
    await init_db()
    
    # 1. Eski webhook va kutilayotgan barcha eski so'rovlarni to'liq tozalash
    await bot.delete_webhook(drop_pending_updates=True)
    
    # 2. Web server hamda Polling-ni parallel holatda ishga tushirish
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot to'xtatildi.")
