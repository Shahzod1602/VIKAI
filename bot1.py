import asyncio
import os
import tempfile
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import google.generativeai as genai

# ================= CONFIG =================
BOT_TOKEN = "8529784413:AAEwqt5BxcVZ-_DGdOpmD3x0w0RGeO3raKI"
GEMINI_API_KEY = "AIzaSyBZkq7UHTAGaG5jaBv2ib4zBM0eXgJN6EQ"
MODEL_NAME = "gemini-2.5-pro"
ADMIN_IDS = [5471121432]  # Admin ID'larni shu yerga qo'ying
DB_FILE = "vikai_bot.db"

bot = Bot(BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
genai.configure(api_key=GEMINI_API_KEY)


# ================= SQLite DATABASE =================
class Database:
    def __init__(self, db_file=DB_FILE):
        self.db_file = db_file
        self.init_database()

    def init_database(self):
        """Database va table'larni yaratish"""
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()

            # Users table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    last_name TEXT,
                    phone TEXT NOT NULL,
                    language TEXT DEFAULT 'uz',
                    username TEXT,
                    joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    transcription_count INTEGER DEFAULT 0,
                    monthly_limit INTEGER DEFAULT 7200,
                    used_seconds INTEGER DEFAULT 0,
                    reset_date TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    is_admin BOOLEAN DEFAULT 0
                )
            ''')

            # Transcriptions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transcriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    duration INTEGER DEFAULT 0,
                    audio_type TEXT,
                    file_size INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')

            # Token stats table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE DEFAULT CURRENT_DATE,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0
                )
            ''')

            # Daily stats table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date DATE PRIMARY KEY,
                    new_users INTEGER DEFAULT 0,
                    total_transcriptions INTEGER DEFAULT 0,
                    total_audio_duration INTEGER DEFAULT 0,
                    daily_active_users INTEGER DEFAULT 0
                )
            ''')

            conn.commit()

    def add_user(self, user_id, first_name, last_name, phone, language, username):
        """Yangi foydalanuvchi qo'shish"""
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()

            # Foydalanuvchi borligini tekshirish
            cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
            existing_user = cursor.fetchone()

            if existing_user:
                return False  # Foydalanuvchi allaqachon mavjud

            # Admin yoki oddiy user?
            is_admin = 1 if user_id in ADMIN_IDS else 0

            # Oylik reset sanasi (joriy oyning 1-sanasi)
            reset_date = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            cursor.execute('''
                INSERT INTO users 
                (user_id, first_name, last_name, phone, language, username, is_admin, reset_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, first_name, last_name, phone, language, username, is_admin, reset_date))

            conn.commit()
            return True  # Yangi foydalanuvchi

    def get_user(self, user_id):
        """Foydalanuvchi ma'lumotlarini olish"""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            user = cursor.fetchone()

            if user:
                return dict(user)
            return None

    def update_user_language(self, user_id, language):
        """Foydalanuvchi tilini yangilash"""
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET language = ? WHERE user_id = ?', (language, user_id))
            conn.commit()

    def update_last_active(self, user_id):
        """Foydalanuvchining oxirgi faolligini yangilash"""
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
            conn.commit()

    def get_user_balance(self, user_id):
        """Foydalanuvchi balansini olish"""
        user = self.get_user(user_id)
        if not user:
            return None

        # Agar admin bo'lsa, cheksiz
        if user['is_admin']:
            return {
                'used': user['used_seconds'],
                'limit': float('inf'),  # Cheksiz
                'remaining': float('inf'),
                'reset_date': user['reset_date']
            }

        # Oyni tekshirish: agar oy o'zgarsa, balansni yangilash
        now = datetime.now()
        reset_date = datetime.fromisoformat(user['reset_date']) if isinstance(user['reset_date'], str) else user[
            'reset_date']

        if now.month != reset_date.month or now.year != reset_date.year:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                new_reset_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                cursor.execute('''
                    UPDATE users 
                    SET used_seconds = 0, reset_date = ? 
                    WHERE user_id = ?
                ''', (new_reset_date, user_id))
                conn.commit()

            return {
                'used': 0,
                'limit': user['monthly_limit'],
                'remaining': user['monthly_limit'],
                'reset_date': new_reset_date
            }

        return {
            'used': user['used_seconds'],
            'limit': user['monthly_limit'],
            'remaining': user['monthly_limit'] - user['used_seconds'],
            'reset_date': reset_date
        }

    def update_user_balance(self, user_id, duration_seconds):
        """Foydalanuvchi balansini yangilash"""
        user = self.get_user(user_id)
        if not user:
            return False

        # Agar admin bo'lsa, limit tekshirish kerak emas
        if user['is_admin']:
            return True

        # Balansni tekshirish
        balance = self.get_user_balance(user_id)
        if balance['remaining'] < duration_seconds:
            return False

        # Balansni yangilash
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users 
                SET used_seconds = used_seconds + ?, transcription_count = transcription_count + 1 
                WHERE user_id = ?
            ''', (duration_seconds, user_id))
            conn.commit()

        # Oxirgi faollikni yangilash
        self.update_last_active(user_id)
        return True

    def add_transcription(self, user_id, duration_seconds, audio_type, file_size=0):
        """Transkripsiya qo'shish"""
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO transcriptions (user_id, duration, audio_type, file_size)
                VALUES (?, ?, ?, ?)
            ''', (user_id, duration_seconds, audio_type, file_size))
            conn.commit()

    def add_token_usage(self, input_tokens, output_tokens):
        """Token statistikasini qo'shish"""
        today = datetime.now().date()

        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()

            # Bugungi statistikani tekshirish
            cursor.execute('SELECT id FROM token_stats WHERE date = ?', (today,))
            existing = cursor.fetchone()

            if existing:
                cursor.execute('''
                    UPDATE token_stats 
                    SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?
                    WHERE date = ?
                ''', (input_tokens, output_tokens, today))
            else:
                cursor.execute('''
                    INSERT INTO token_stats (date, input_tokens, output_tokens)
                    VALUES (?, ?, ?)
                ''', (today, input_tokens, output_tokens))

            conn.commit()

    def get_daily_stats(self, date=None):
        """Kunlik statistika"""
        if date is None:
            date = datetime.now().date() - timedelta(days=1)

        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Kunlik statistikani olish yoki yaratish
            cursor.execute('SELECT * FROM daily_stats WHERE date = ?', (date,))
            stats = cursor.fetchone()

            if not stats:
                # Yangi kun uchun statistika hisoblash

                # Yangi foydalanuvchilar
                cursor.execute('''
                    SELECT COUNT(*) as new_users 
                    FROM users 
                    WHERE DATE(joined_date) = ?
                ''', (date,))
                new_users = cursor.fetchone()[0] or 0

                # Transkripsiyalar
                cursor.execute('''
                    SELECT 
                        COUNT(*) as total_transcriptions,
                        SUM(duration) as total_audio_duration
                    FROM transcriptions 
                    WHERE DATE(timestamp) = ?
                ''', (date,))
                trans_result = cursor.fetchone()
                total_transcriptions = trans_result[0] or 0
                total_audio_duration = trans_result[1] or 0

                # Faol foydalanuvchilar
                cursor.execute('''
                    SELECT COUNT(DISTINCT user_id) as daily_active_users
                    FROM transcriptions 
                    WHERE DATE(timestamp) = ?
                ''', (date,))
                daily_active_users = cursor.fetchone()[0] or 0

                # Jami foydalanuvchilar
                cursor.execute('SELECT COUNT(*) as total_users FROM users')
                total_users = cursor.fetchone()[0] or 0

                # Statistikani saqlash
                cursor.execute('''
                    INSERT INTO daily_stats 
                    (date, new_users, total_transcriptions, total_audio_duration, daily_active_users)
                    VALUES (?, ?, ?, ?, ?)
                ''', (date, new_users, total_transcriptions, total_audio_duration, daily_active_users))

                stats = {
                    'date': date.strftime("%m/%d/%Y"),
                    'new_users': new_users,
                    'total_transcriptions': total_transcriptions,
                    'total_audio_duration': total_audio_duration,
                    'daily_active_users': daily_active_users,
                    'total_users': total_users
                }
            else:
                stats = dict(stats)
                stats['date'] = date.strftime("%m/%d/%Y")

                # Jami foydalanuvchilar
                cursor.execute('SELECT COUNT(*) as total_users FROM users')
                stats['total_users'] = cursor.fetchone()[0] or 0

            conn.commit()
            return stats

    def get_token_stats(self):
        """Token statistikasini olish"""
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()

            # Barcha tokenlarni yig'ish
            cursor.execute('''
                SELECT 
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output
                FROM token_stats
            ''')

            result = cursor.fetchone()
            total_input = result[0] or 0
            total_output = result[1] or 0

            return {
                'input_tokens': total_input,
                'output_tokens': total_output,
                'total_tokens': total_input + total_output
            }

    def get_all_users(self):
        """Barcha foydalanuvchilarni olish"""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users ORDER BY joined_date DESC')
            return [dict(row) for row in cursor.fetchall()]


# Database obyektini yaratish
db = Database()


# ================= STATES =================
class UserStates(StatesGroup):
    waiting_for_contact = State()
    waiting_for_language = State()


# ================= KEYBOARDS =================
def get_contact_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="📱 Iltimos, kontaktni ulashing", request_contact=True)
        ]],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_language_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇺🇿 O'zbekcha")],
            [KeyboardButton(text="🇷🇺 Русский")],
            [KeyboardButton(text="🇬🇧 English")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_main_menu_keyboard(language="uz"):
    if language == "ru":
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎤 Голос в текст")],
                [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📊 Статистика")],
                [KeyboardButton(text="ℹ️ О боте"), KeyboardButton(text="👨‍💻 Поддержка")]
            ],
            resize_keyboard=True
        )
    elif language == "en":
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎤 Voice to Text")],
                [KeyboardButton(text="💰 Balance"), KeyboardButton(text="📊 Statistics")],
                [KeyboardButton(text="ℹ️ About"), KeyboardButton(text="👨‍💻 Support")]
            ],
            resize_keyboard=True
        )
    else:  # uz
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎤 Ovozdan matnga")],
                [KeyboardButton(text="💰 Balans"), KeyboardButton(text="📊 Statistika")],
                [KeyboardButton(text="ℹ️ Bot haqida"), KeyboardButton(text="👨‍💻 Yordam")]
            ],
            resize_keyboard=True
        )


# ================= COMMAND HANDLERS =================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id

    user = db.get_user(user_id)
    if user:
        language = user['language']
        welcome_messages = {
            "uz": f"Assalomu alaykum! VIKAI botiga xush kelibsiz! 😊\n\n👉 /stt - Ovozni matnga aylantirish",
            "ru": f"Здравствуйте! Добро пожаловать в бот VIKAI! 😊\n\n👉 /stt - Преобразование голоса в текст",
            "en": f"Hello! Welcome to VIKAI bot! 😊\n\n👉 /stt - Voice to text conversion"
        }
        await message.answer(
            welcome_messages.get(language, welcome_messages["uz"]),
            reply_markup=get_main_menu_keyboard(language)
        )
        await state.clear()
    else:
        await message.answer(
            "📱 Iltimos, kontaktni ulashing",
            reply_markup=get_contact_keyboard()
        )
        await state.set_state(UserStates.waiting_for_contact)


@dp.message(UserStates.waiting_for_contact, F.contact)
async def process_contact(message: Message, state: FSMContext):
    contact = message.contact

    await state.update_data(
        user_id=message.from_user.id,
        phone=contact.phone_number,
        first_name=contact.first_name or message.from_user.first_name,
        last_name=contact.last_name or message.from_user.last_name
    )

    await message.answer(
        "Tilni tanlang / Выберите язык / Choose language:",
        reply_markup=get_language_keyboard()
    )
    await state.set_state(UserStates.waiting_for_language)


@dp.message(UserStates.waiting_for_language)
async def process_language(message: Message, state: FSMContext):
    language_map = {
        "🇺🇿 O'zbekcha": "uz",
        "🇷🇺 Русский": "ru",
        "🇬🇧 English": "en"
    }

    if message.text not in language_map:
        await message.answer("Iltimos, tugmalardan birini tanlang:")
        return

    user_data = await state.get_data()
    language = language_map[message.text]
    user_id = user_data['user_id']

    is_admin = (user_id in ADMIN_IDS)

    is_new_user = db.add_user(
        user_id=user_id,
        first_name=user_data['first_name'],
        last_name=user_data['last_name'],
        phone=user_data['phone'],
        language=language,
        username=message.from_user.username
    )

    welcome_messages = {
        "uz": f"Assalomu alaykum! VIKAI botiga xush kelibsiz! 😊\n\n"
              f"{'⚡ Siz adminsiz! Cheksiz foydalaning.' if is_admin else '💰 Sizning oylik limit: 2 soat (7200 soniya)'}\n\n"
              f"👉 /stt - Ovozni matnga aylantirish",
        "ru": f"Здравствуйте! Добро пожаловать в бот VIKAI! 😊\n\n"
              f"{'⚡ Вы админ! Используйте без ограничений.' if is_admin else '💰 Ваш месячный лимит: 2 часа (7200 секунд)'}\n\n"
              f"👉 /stt - Преобразование голоса в текст",
        "en": f"Hello! Welcome to VIKAI bot! 😊\n\n"
              f"{'⚡ You are admin! Unlimited usage.' if is_admin else '💰 Your monthly limit: 2 hours (7200 seconds)'}\n\n"
              f"👉 /stt - Voice to text conversion"
    }

    await message.answer(
        welcome_messages.get(language, welcome_messages["uz"]),
        reply_markup=get_main_menu_keyboard(language)
    )
    await state.clear()


# ================= ADMIN STATISTICS =================
@dp.message(Command("adminstats"))
@dp.message(F.text == "📊 Admin")
async def cmd_adminstats(message: Message):
    user_id = message.from_user.id

    if user_id not in ADMIN_IDS:
        await message.answer("❌ Bu buyruq faqat adminlar uchun!")
        return

    stats = db.get_daily_stats()
    token_stats = db.get_token_stats()

    # Audio uzunligi
    minutes = stats['total_audio_duration'] // 60
    seconds = stats['total_audio_duration'] % 60
    audio_length = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    # Tokenlar
    input_tokens = token_stats['input_tokens'] / 1000
    output_tokens = token_stats['output_tokens'] / 1000
    total_tokens = token_stats['total_tokens'] / 1000

    # Foydalanuvchilar balansi
    users = db.get_all_users()
    active_users = 0
    low_balance_users = 0

    for user in users:
        if not user['is_admin']:
            balance = db.get_user_balance(user['user_id'])
            if balance and balance['remaining'] < 1800:  # 30 daqiqadan kam
                low_balance_users += 1
            if user['transcription_count'] > 0:
                active_users += 1

    report = (
        "🤖 *VikAI - Admin Statistika*\n\n"
        f"📅 Sana: {stats['date']}\n"
        f"👥 Yangi foydalanuvchilar: {stats['new_users']}\n"
        f"🎤 Jami transkripsiyalar: {stats['total_transcriptions']}\n"
        f"⏱️ Jami audio uzunligi: {audio_length}\n"
        f"👥 Kunlik faol foydalanuvchilar: {stats['daily_active_users']}\n"
        f"📊 Jami foydalanuvchilar: {stats['total_users']}\n\n"
        f"💰 *Balanslar:*\n"
        f"• Faol foydalanuvchilar: {active_users}\n"
        f"• Kam balansli: {low_balance_users}\n\n"
        "🔧 *AI Foydalanish:*\n"
        f"📥 Kirish tokenlari: {input_tokens:.1f}K\n"
        f"📤 Chiqish tokenlari: {output_tokens:.1f}K\n"
        f"📈 Jami tokenlar: {total_tokens:.1f}K"
    )

    await message.answer(report, parse_mode="Markdown")


# ================= USER BALANCE =================
@dp.message(Command("balance"))
@dp.message(F.text == "💰 Balans")
@dp.message(F.text == "💰 Баланс")
@dp.message(F.text == "💰 Balance")
async def cmd_balance(message: Message):
    user_id = message.from_user.id

    user = db.get_user(user_id)
    if not user:
        await message.answer("Avval /start bosing!")
        return

    language = user['language']

    # Admin uchun
    if user['is_admin']:
        admin_messages = {
            "uz": "⚡ *Admin paneli*\n\nSiz adminsiz! Cheksiz foydalanishingiz mumkin.",
            "ru": "⚡ *Админ панель*\n\nВы администратор! Можете использовать без ограничений.",
            "en": "⚡ *Admin Panel*\n\nYou are admin! Unlimited usage."
        }
        await message.answer(
            admin_messages.get(language, admin_messages["uz"]),
            parse_mode="Markdown"
        )
        return

    # Oddiy user uchun balans
    balance = db.get_user_balance(user_id)
    if not balance:
        await message.answer("❌ Balans ma'lumotlari topilmadi")
        return

    # Formatlash
    used_minutes = balance['used'] // 60
    used_seconds = balance['used'] % 60
    remaining_minutes = balance['remaining'] // 60
    remaining_seconds = balance['remaining'] % 60
    limit_minutes = balance['limit'] // 60

    reset_date = balance['reset_date']
    if isinstance(reset_date, str):
        next_reset = datetime.strptime(reset_date[:10], "%Y-%m-%d")
    else:
        next_reset = balance['reset_date']

    # Keyingi reset sanasi (keyingi oyning 1-sanasi)
    if next_reset.month == 12:
        next_reset = next_reset.replace(year=next_reset.year + 1, month=1)
    else:
        next_reset = next_reset.replace(month=next_reset.month + 1)

    balance_messages = {
        "uz": f"💰 *Sizning balansingiz*\n\n"
              f"📊 Oylik limit: {limit_minutes} soat ({balance['limit']} soniya)\n"
              f"⏳ Ishlatilgan: {used_minutes}min {used_seconds}sek\n"
              f"✅ Qolgan: {remaining_minutes}min {remaining_seconds}sek\n"
              f"📅 Keyingi yangilanish: {next_reset.strftime('%d.%m.%Y')}\n\n"
              f"ℹ️ Balans har oyning 1-sanasi yangilanadi.",
        "ru": f"💰 *Ваш баланс*\n\n"
              f"📊 Месячный лимит: {limit_minutes} часов ({balance['limit']} секунд)\n"
              f"⏳ Использовано: {used_minutes}мин {used_seconds}сек\n"
              f"✅ Осталось: {remaining_minutes}мин {remaining_seconds}сек\n"
              f"📅 Следующее обновление: {next_reset.strftime('%d.%m.%Y')}\n\n"
              f"ℹ️ Баланс обновляется 1-го числа каждого месяца.",
        "en": f"💰 *Your Balance*\n\n"
              f"📊 Monthly limit: {limit_minutes} hours ({balance['limit']} seconds)\n"
              f"⏳ Used: {used_minutes}min {used_seconds}sec\n"
              f"✅ Remaining: {remaining_minutes}min {remaining_seconds}sec\n"
              f"📅 Next reset: {next_reset.strftime('%d.%m.%Y')}\n\n"
              f"ℹ️ Balance resets on the 1st of each month."
    }

    await message.answer(
        balance_messages.get(language, balance_messages["uz"]),
        parse_mode="Markdown"
    )


# ================= AUDIO HANDLER =================
@dp.message(F.voice | F.audio)
async def handle_full_audio(message: Message):
    user_id = message.from_user.id

    user = db.get_user(user_id)
    if not user:
        await message.answer("Iltimos, avval /start komandasini bosing!")
        return

    language = user['language']

    # Fayl ma'lumotlari
    if message.voice:
        duration = message.voice.duration or 0
    else:
        duration = message.audio.duration or 0

    # Balansni tekshirish (faqat oddiy userlar uchun)
    if not user['is_admin']:
        if not db.update_user_balance(user_id, duration):
            balance = db.get_user_balance(user_id)

            error_messages = {
                "uz": f"❌ *Balans yetarli emas!*\n\n"
                      f"Sizda {balance['remaining'] // 60}min {balance['remaining'] % 60}sek qolgan.\n"
                      f"Kerak bo'ladigan: {duration // 60}min {duration % 60}sek\n\n"
                      f"💰 /balance - Balansni tekshirish",
                "ru": f"❌ *Недостаточно баланса!*\n\n"
                      f"У вас осталось {balance['remaining'] // 60}мин {balance['remaining'] % 60}сек.\n"
                      f"Требуется: {duration // 60}мин {duration % 60}сек\n\n"
                      f"💰 /balance - Проверить баланс",
                "en": f"❌ *Insufficient balance!*\n\n"
                      f"You have {balance['remaining'] // 60}min {balance['remaining'] % 60}sec left.\n"
                      f"Required: {duration // 60}min {duration % 60}sec\n\n"
                      f"💰 /balance - Check balance"
            }
            await message.answer(error_messages.get(language, error_messages["uz"]), parse_mode="Markdown")
            return

    # Audio processing
    processing_messages = {
        "uz": "📥 Audio yuklanmoqda...",
        "ru": "📥 Загрузка аудио...",
        "en": "📥 Loading audio..."
    }

    status_msg = await message.reply(processing_messages.get(language, "📥 Audio yuklanmoqda..."))
    temp_dir = tempfile.mkdtemp()

    try:
        if message.voice:
            file_id = message.voice.file_id
            file_ext = "ogg"
            audio_type = "voice"
        else:
            file_id = message.audio.file_id
            file_name = message.audio.file_name or "audio"
            file_ext = file_name.split('.')[-1]
            audio_type = "audio"

        file_path = os.path.join(temp_dir, f"temp_audio.{file_ext}")
        file_info = await bot.get_file(file_id)

        if file_info.file_size > 30 * 1024 * 1024:
            await status_msg.edit_text("❌ Fayl hajmi 20MB dan katta.")
            return

        await bot.download_file(file_info.file_path, file_path)

        working_messages = {
            "uz": "🧠 AI tahlil qilmoqda...",
            "ru": "🧠 AI анализирует...",
            "en": "🧠 AI is analyzing..."
        }
        await status_msg.edit_text(working_messages.get(language, "🧠 AI tahlil qilmoqda..."))

        # Gemini processing
        loop = asyncio.get_event_loop()
        g_file = await loop.run_in_executor(None, genai.upload_file, file_path)

        while g_file.state.name == "PROCESSING":
            await asyncio.sleep(2)
            g_file = await loop.run_in_executor(None, genai.get_file, g_file.name)

        if g_file.state.name == "FAILED":
            raise Exception("Gemini faylni o'qiy olmadi.")

        model = genai.GenerativeModel(MODEL_NAME)
        response = await loop.run_in_executor(
            None,
            model.generate_content,
            [
                "Please transcribe this audio file accurately. Do not summarize, give full text.",
                g_file
            ]
        )

        # Ma'lumotlarni saqlash
        db.add_transcription(user_id, duration, audio_type, file_info.file_size)

        # Token statistikasi
        text = response.text
        estimated_input_tokens = len(text.split()) * 1.3
        estimated_output_tokens = len(text.split())
        db.add_token_usage(estimated_input_tokens, estimated_output_tokens)

        # Balans ma'lumoti
        if not user['is_admin']:
            balance = db.get_user_balance(user_id)
            remaining_messages = {
                "uz": f"\n\n💰 Qolgan balans: {balance['remaining'] // 60}min {balance['remaining'] % 60}sek",
                "ru": f"\n\n💰 Остаток баланса: {balance['remaining'] // 60}мин {balance['remaining'] % 60}сек",
                "en": f"\n\n💰 Remaining balance: {balance['remaining'] // 60}min {balance['remaining'] % 60}sec"
            }
            text += remaining_messages.get(language, remaining_messages["uz"])

        await status_msg.delete()

        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await message.reply(text[i:i + 4000])
        else:
            await message.reply(text)

        await loop.run_in_executor(None, genai.delete_file, g_file.name)

    except Exception as e:
        error_messages = {
            "uz": f"❌ Xatolik: {str(e)}",
            "ru": f"❌ Ошибка: {str(e)}",
            "en": f"❌ Error: {str(e)}"
        }
        await status_msg.edit_text(error_messages.get(language, f"❌ Xatolik: {str(e)}"))

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        os.rmdir(temp_dir)


# ================= OTHER COMMANDS =================
@dp.message(Command("stt"))
async def cmd_stt(message: Message):
    user_id = message.from_user.id

    user = db.get_user(user_id)
    if not user:
        await message.answer("Iltimos, avval /start komandasini bosing!")
        return

    language = user['language']
    messages = {
        "uz": "🎤 *Ovozni matnga aylantirish*\n\nOvozli xabar yoki audio fayl yuboring. Men uni matnga aylantiraman.",
        "ru": "🎤 *Преобразование голоса в текст*\n\nОтправьте голосовое сообщение или аудиофайл. Я преобразую его в текст.",
        "en": "🎤 *Voice to Text Conversion*\n\nSend a voice message or audio file. I will convert it to text."
    }

    await message.answer(
        messages.get(language, messages["uz"]),
        parse_mode="Markdown"
    )


@dp.message(Command("users"))
async def cmd_users(message: Message):
    """Admin uchun foydalanuvchilar ro'yxati"""
    user_id = message.from_user.id

    if user_id not in ADMIN_IDS:
        await message.answer("❌ Bu buyruq faqat adminlar uchun!")
        return

    users = db.get_all_users()

    if not users:
        await message.answer("❌ Hozircha foydalanuvchilar yo'q")
        return

    response = "👥 *Foydalanuvchilar ro'yxati:*\n\n"

    for i, user in enumerate(users[:50], 1):  # Faqat birinchi 50 tasi
        balance = db.get_user_balance(user['user_id'])
        remaining_min = balance['remaining'] // 60 if balance else 0

        response += f"{i}. {user['first_name']} {user['last_name'] or ''}\n"
        response += f"   👤 @{user['username'] or 'yoq'}\n"
        response += f"   📞 {user['phone']}\n"
        response += f"   📊 Transkripsiyalar: {user['transcription_count']}\n"
        response += f"   💰 Qolgan: {remaining_min}min\n"
        response += f"   📅 Ro'yxatdan: {user['joined_date'][:10] if isinstance(user['joined_date'], str) else user['joined_date'].strftime('%Y-%m-%d')}\n"
        response += "   " + ("⚡ Admin\n" if user['is_admin'] else "👤 User\n")
        response += "\n"

    if len(users) > 50:
        response += f"\n... va yana {len(users) - 50} foydalanuvchi"

    await message.answer(response, parse_mode="Markdown")


# ================= RUN =================
async def main():
    print("🎤 VIKAI bot ishga tushdi...")
    print(f"📊 Database fayli: {DB_FILE}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())