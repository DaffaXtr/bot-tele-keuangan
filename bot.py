import os
import sqlite3
import logging
import re
import math
import csv
from datetime import datetime, time

from dotenv import load_dotenv
load_dotenv()  # Memuat environment variables dari file .env

import matplotlib
matplotlib.use("Agg")  # Non-GUI backend untuk thread safety
import matplotlib.pyplot as plt

from fpdf import FPDF
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Logging untuk pemantauan status bot di terminal
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# State untuk ConversationHandler
(
    KATEGORI_STATE,
    NOMINAL_STATE,
    SET_BUDGET_STATE,
    CUSTOM_DATE_STATE,
    EDIT_NOMINAL_STATE,
    EDIT_KET_STATE,
    ADD_CAT_NAME_STATE,
) = range(1, 8)

# Token Bot Telegram & Konfigurasi Admin Utama
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAIN_ADMIN_USERNAME = os.getenv("MAIN_ADMIN_USERNAME", "dapxtr").strip().lstrip("@").lower()
MAIN_ADMIN_ID_RAW = os.getenv("MAIN_ADMIN_ID", "").strip()

if not TOKEN:
    print("❌ ERROR: TELEGRAM_BOT_TOKEN tidak ditemukan di berkas .env! Silakan isi TELEGRAM_BOT_TOKEN Anda.")
    exit(1)

DB_NAME = "keuangan.db"
PER_PAGE = 5  # Item per halaman di riwayat

# Daftar Kategori Preset Default
DEFAULT_PENGELUARAN_KATEGORI = ["🍔 Makanan", "🚗 Transportasi", "🛍️ Belanja", "🏠 Tagihan", "🎬 Hiburan", "💊 Kesehatan", "📦 Lainnya"]
DEFAULT_PEMASUKAN_KATEGORI = ["💼 Gaji", "🎁 Bonus", "📈 Investasi", "💵 Usaha", "📦 Lainnya"]

def is_main_admin_user(user) -> bool:
    if not user:
        return False
    if MAIN_ADMIN_ID_RAW and str(user.id) == MAIN_ADMIN_ID_RAW:
        return True
    return bool(MAIN_ADMIN_USERNAME and user.username and user.username.lower() == MAIN_ADMIN_USERNAME)

def upsert_user(user) -> None:
    if not user:
        return
    role = "admin" if is_main_admin_user(user) else "user"
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, role, status, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                role = CASE WHEN excluded.role = 'admin' THEN 'admin' ELSE users.role END,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (user.id, user.username, user.first_name, user.last_name, role),
        )
        conn.commit()

def get_user_status(user_id: int) -> str:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return row[0] if row else "active"

def get_user_role(user_id: int) -> str:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return row[0] if row else "user"

def is_admin_id(user_id: int) -> bool:
    return get_user_role(user_id) == "admin"

# Middleware otorisasi multi-user: semua user aktif boleh memakai bot.
async def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False

    upsert_user(user)
    if get_user_status(user.id) != "active":
        msg = (
            "🔒 <b>Akses Ditolak!</b>\n\n"
            "Akun Anda sedang diblokir oleh admin utama."
        )
        if update.callback_query:
            await update.callback_query.answer("🔒 Akun diblokir.", show_alert=True)
        elif update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return False
    return True

async def is_admin_authorized(update: Update) -> bool:
    if not await is_authorized(update):
        return False
    user = update.effective_user
    if user and is_admin_id(user.id):
        return True

    msg = "🔒 Fitur ini hanya untuk admin utama."
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    elif update.message:
        await update.message.reply_text(msg)
    return False

# Helper Pembersih Emoji untuk Label Matplotlib (Menghindari Karakter Kotak [?])
def strip_emoji(text: str) -> str:
    clean = re.sub(r'[^\w\s-]', '', text).strip()
    return clean if clean else text

# Class untuk Dokumen PDF Report
class PDFReport(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, "Laporan Keuangan Pribadi", border=False, new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font("Helvetica", "I", 9)
        self.cell(0, 5, f"Tanggal Cetak: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}", border=False, new_x="LMARGIN", new_y="NEXT", align="C")
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Halaman {self.page_no()}/{{nb}}", align="C")

# Keyboard Utama
def get_main_keyboard(user_id: int = None):
    keyboard = [
        [KeyboardButton("📊 Rekap Keuangan"), KeyboardButton("📈 Grafik Visual")],
        [KeyboardButton("📋 Riwayat Transaksi"), KeyboardButton("🎯 Set Budget")],
        [KeyboardButton("📥 Tambah Pemasukan"), KeyboardButton("📤 Tambah Pengeluaran")],
        [KeyboardButton("📑 Export Laporan"), KeyboardButton("⚙️ Pengaturan Saya")],
        [KeyboardButton("❓ Bantuan")],
        [KeyboardButton("❌ Batal")]
    ]
    if user_id and is_admin_id(user_id):
        keyboard.insert(-2, [KeyboardButton("🛡️ Admin Utama"), KeyboardButton("📦 Backup DB")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Format Angka ke Rupiah Indonesia
def format_rupiah(nominal: float) -> str:
    val = int(round(nominal))
    formatted = f"{val:,}".replace(",", ".")
    return f"Rp {formatted}"

# Smart Nominal & Keterangan Parser Pintar Bahasa Indonesia
def parse_nominal_and_keterangan(text: str):
    s = text.strip()
    pattern = r"^(\d+(?:[.,]\d+)?)\s*(k|rb|ribu|jt|juta|m|miliar)?(?:\s+(.*))?$"
    match = re.match(pattern, s, re.IGNORECASE)
    
    if not match:
        raise ValueError("Format nominal tidak dikenali")
        
    raw_num = match.group(1)
    unit = (match.group(2) or "").lower()
    keterangan = (match.group(3) or "").strip()
    
    multiplier = 1.0
    if unit in ["k", "rb", "ribu"]:
        multiplier = 1_000.0
    elif unit in ["jt", "juta", "m"]:
        multiplier = 1_000_000.0
    elif unit in ["miliar"]:
        multiplier = 1_000_000_000.0
        
    if "." in raw_num and "," in raw_num:
        raw_num = raw_num.replace(".", "").replace(",", ".")
    elif "." in raw_num:
        parts = raw_num.split(".")
        if len(parts[-1]) == 3 and len(parts) > 1:
            raw_num = raw_num.replace(".", "")
        else:
            pass
    elif "," in raw_num:
        raw_num = raw_num.replace(",", ".")
        
    try:
        nominal = float(raw_num) * multiplier
    except ValueError:
        raise ValueError("Angka tidak valid")

    if nominal <= 0:
        raise ValueError("Nominal harus lebih dari 0")
        
    ket = keterangan if keterangan else "Tanpa Keterangan"
    return nominal, ket

# Smart Nominal Parser tunggal
def parse_nominal(raw_str: str) -> float:
    nom, _ = parse_nominal_and_keterangan(raw_str)
    return nom

# Inisialisasi Database, Migrasi, & Optimalisasi Indeks
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transaksi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                jenis TEXT,
                nominal REAL,
                keterangan TEXT,
                tanggal TIMESTAMP,
                kategori TEXT DEFAULT 'Umum'
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                role TEXT DEFAULT 'user',
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP
            )
        """)

        cursor.execute("PRAGMA table_info(transaksi)")
        columns = [column[1] for column in cursor.fetchall()]
        if "kategori" not in columns:
            cursor.execute("ALTER TABLE transaksi ADD COLUMN kategori TEXT DEFAULT 'Umum'")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_tgl ON transaksi(user_id, tanggal)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_jenis ON transaksi(user_id, jenis)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                budget_bulanan REAL DEFAULT 0,
                reminder_enabled INTEGER DEFAULT 1
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS custom_kategori (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                jenis TEXT,
                nama_kategori TEXT
            )
        """)

        cursor.execute("""
            INSERT OR IGNORE INTO users (user_id, role, status, created_at)
            SELECT DISTINCT user_id,
                   CASE WHEN CAST(user_id AS TEXT) = ? THEN 'admin' ELSE 'user' END,
                   'active',
                   CURRENT_TIMESTAMP
            FROM transaksi
            WHERE user_id IS NOT NULL
        """, (MAIN_ADMIN_ID_RAW,))
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
        conn.commit()

# Ambil Daftar Kategori Gabungan
def get_user_categories(user_id: int, jenis: str) -> list:
    defaults = DEFAULT_PEMASUKAN_KATEGORI if jenis == "Pemasukan" else DEFAULT_PENGELUARAN_KATEGORI
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nama_kategori FROM custom_kategori WHERE user_id = ? AND jenis = ?", (user_id, jenis))
        customs = [row[0] for row in cursor.fetchall()]
    return defaults + customs

# Perintah /start dan Bantuan
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    user = update.effective_user.first_name if update.effective_user else "Pengguna"
    pesan = (
        f"Halo <b>{user}</b>! Selamat datang di <b>Bot Catatan Keuangan Pribadi</b>. 💰\n\n"
        "Gunakan <b>Tombol Menu</b> di bawah area ketik untuk memilih fitur dengan cepat!\n\n"
        "<b>📌 Pilihan Menu:</b>\n"
        "• 📥 <b>Tambah Pemasukan</b> - Catat uang masuk per kategori\n"
        "• 📤 <b>Tambah Pengeluaran</b> - Catat pengeluaran per kategori\n"
        "• 📊 <b>Rekap Keuangan</b> - Cek total saldo & rincian per kategori\n"
        "• 📈 <b>Grafik Visual</b> - Lihat grafik Pemasukan, Pengeluaran & Cashflow\n"
        "• 📋 <b>Riwayat Transaksi</b> - Edit & Hapus transaksi langsung\n"
        "• 🎯 <b>Set Budget</b> - Peringatan sisa anggaran bulanan\n"
        "• ⚙️ <b>Pengaturan Saya</b> - Tambah/Hapus kategori kustom Anda\n"
        "• 📑 <b>Export Laporan</b> - Unduh laporan Excel (CSV) & PDF\n"
        "• 🛡️ <b>Admin Utama</b> - Kelola user dan backup database (khusus admin)"
    )
    await update.message.reply_text(
        pesan,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

# Helper Penangani Tombol Menu Utama
async def handle_if_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not await is_authorized(update):
        return True

    if text == "❌ Batal" or text == "/batal":
        await batal(update, context)
        return True
    elif text == "📊 Rekap Keuangan":
        await rekap(update, context)
        return True
    elif text in ["📈 Grafik Visual", "/grafik"]:
        await prompt_grafik_menu(update, context)
        return True
    elif text == "📋 Riwayat Transaksi":
        await riwayat(update, context)
        return True
    elif text == "📥 Tambah Pemasukan":
        await prompt_pemasukan(update, context)
        return True
    elif text == "📤 Tambah Pengeluaran":
        await prompt_pengeluaran(update, context)
        return True
    elif text == "🎯 Set Budget":
        await prompt_set_budget(update, context)
        return True
    elif text in ["⚙️ Admin & Kelola", "⚙️ Pengaturan Saya"]:
        await admin_panel(update, context)
        return True
    elif text == "🛡️ Admin Utama":
        await main_admin_panel(update, context)
        return True
    elif text in ["📑 Export Laporan", "📊 Export CSV"]:
        await prompt_export_menu(update, context)
        return True
    elif text == "📦 Backup DB":
        await backup_db(update, context)
        return True
    elif text in ["❓ Bantuan", "/help", "/bantuan", "/start"]:
        await start(update, context)
        return True
    return False

# --- ALUR TAMBAH TRANSAKSI & PILIH KATEGORI ---
def get_category_keyboard(user_id: int, jenis: str):
    cats = get_user_categories(user_id, jenis)
    buttons = []
    row = []
    for i, cat in enumerate(cats):
        row.append(InlineKeyboardButton(cat, callback_data=f"selectcat_{i}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="cancel_conv")])
    return InlineKeyboardMarkup(buttons)

async def prompt_pemasukan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    context.user_data["jenis"] = "Pemasukan"
    await update.message.reply_text(
        "📥 <b>Pencatatan Pemasukan</b>\n\nPilih <b>Kategori Pemasukan</b> di bawah ini:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_category_keyboard(user_id, "Pemasukan")
    )
    return KATEGORI_STATE

async def prompt_pengeluaran(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    context.user_data["jenis"] = "Pengeluaran"
    await update.message.reply_text(
        "📤 <b>Pencatatan Pengeluaran</b>\n\nPilih <b>Kategori Pengeluaran</b> di bawah ini:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_category_keyboard(user_id, "Pengeluaran")
    )
    return KATEGORI_STATE

async def select_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "cancel_conv":
        await query.edit_message_text("👌 Operasi dibatalkan.")
        return ConversationHandler.END

    user_id = query.from_user.id
    idx = int(data.split("_")[1])
    jenis = context.user_data.get("jenis", "Pengeluaran")
    cats = get_user_categories(user_id, jenis)
    selected_cat = cats[idx] if idx < len(cats) else "Umum"

    context.user_data["kategori"] = selected_cat

    emoji = "📥" if jenis == "Pemasukan" else "📤"
    await query.edit_message_text(
        f"{emoji} <b>Kategori Terpilih:</b> {selected_cat}\n\n"
        "Silakan kirim nominal dan keterangan transaksi Anda.\n"
        "<i>Contoh:</i> <code>1,5 juta Sangu</code>, <code>1.5jt Gaji</code>, atau <code>15k Makan Siang</code>\n\n"
        "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
        parse_mode=ParseMode.HTML
    )
    return NOMINAL_STATE

async def simpan_transaksi_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    try:
        nominal, keterangan = parse_nominal_and_keterangan(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Nominal tidak valid! Harap masukkan angka yang benar (misal: <code>1,5 juta Sangu</code>, <code>1.5jt Gaji</code>, <code>15k Makan</code>, atau <code>15.000</code>).\n"
            "Silakan coba kirim ulang atau tekan <b>❌ Batal</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
        return NOMINAL_STATE

    user_id = update.effective_user.id
    jenis = context.user_data.get("jenis", "Pengeluaran")
    kategori = context.user_data.get("kategori", "Umum")
    waktu_sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transaksi (user_id, jenis, nominal, keterangan, tanggal, kategori) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, jenis, nominal, keterangan, waktu_sekarang, kategori)
        )
        tx_id = cursor.lastrowid
        conn.commit()

    emoji = "📥" if jenis == "Pemasukan" else "📤"
    pesan_sukses = (
        f"✅ <b>{jenis} Berhasil Dicatat!</b> #{tx_id}\n\n"
        f"{emoji} <b>Nominal:</b> {format_rupiah(nominal)}\n"
        f"🏷️ <b>Kategori:</b> {kategori}\n"
        f"📝 <b>Keterangan:</b> {keterangan}\n"
        f"📅 <b>Waktu:</b> {waktu_sekarang}"
    )

    if jenis == "Pengeluaran":
        warning_msg = check_budget_warning(user_id)
        if warning_msg:
            pesan_sukses += f"\n\n{warning_msg}"

    await update.message.reply_text(
        pesan_sukses,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

# Helper Cek Peringatan Budget
def check_budget_warning(user_id: int) -> str:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT budget_bulanan FROM settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row or not row[0] or row[0] <= 0:
            return ""

        budget = row[0]
        cursor.execute(
            "SELECT SUM(nominal) FROM transaksi WHERE user_id = ? AND jenis = 'Pengeluaran' AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')",
            (user_id,)
        )
        total_keluar = cursor.fetchone()[0] or 0.0

    if total_keluar >= budget:
        return f"🚨 <b>PERINGATAN BUDGET!</b> Total pengeluaran bulan ini ({format_rupiah(total_keluar)}) telah <b>MELEBIHI</b> batas budget bulanan Anda ({format_rupiah(budget)})!"
    elif total_keluar >= (budget * 0.8):
        persen = int((total_keluar / budget) * 100)
        return f"⚠️ <b>PERINGATAN BUDGET!</b> Pengeluaran bulan ini ({format_rupiah(total_keluar)}) sudah mencapai <b>{persen}%</b> dari target budget Anda ({format_rupiah(budget)})."
    return ""

async def batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👌 Operasi dibatalkan.",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

# --- FITUR REKAP & BREAKDOWN KATEGORI ---
def get_rekap_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📅 Hari Ini", callback_data="rekap_today"),
            InlineKeyboardButton("🗓️ Bulan Ini", callback_data="rekap_this_month"),
        ],
        [
            InlineKeyboardButton("📆 Pilih Bulan/Tahun", callback_data="rekap_custom_prompt"),
            InlineKeyboardButton("♾️ Semua Waktu", callback_data="rekap_all"),
        ],
        [
            InlineKeyboardButton("📈 Menu Grafik Visual", callback_data="chart_menu_prompt")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_rekap_data(user_id: int, period: str = "this_month", custom_ym: str = None):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()

        if period == "today":
            judul = f"Hari Ini ({datetime.now().strftime('%d-%m-%Y')})"
            where_clause = "user_id = ? AND strftime('%Y-%m-%d', tanggal) = strftime('%Y-%m-%d', 'now', 'localtime')"
            params = (user_id,)
        elif period == "this_month":
            judul = f"Bulan Ini ({datetime.now().strftime('%m-%Y')})"
            where_clause = "user_id = ? AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')"
            params = (user_id,)
        elif period == "all":
            judul = "Semua Waktu"
            where_clause = "user_id = ?"
            params = (user_id,)
        elif period == "custom" and custom_ym:
            judul = f"Periode {custom_ym}"
            where_clause = "user_id = ? AND strftime('%Y-%m', tanggal) = ?"
            params = (user_id, custom_ym)
        else:
            judul = "Bulan Ini"
            where_clause = "user_id = ? AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')"
            params = (user_id,)

        cursor.execute(f"SELECT SUM(nominal) FROM transaksi WHERE {where_clause} AND jenis = 'Pemasukan'", params)
        total_masuk = cursor.fetchone()[0] or 0.0

        cursor.execute(f"SELECT SUM(nominal) FROM transaksi WHERE {where_clause} AND jenis = 'Pengeluaran'", params)
        total_keluar = cursor.fetchone()[0] or 0.0

        cursor.execute(
            f"SELECT kategori, SUM(nominal) FROM transaksi WHERE {where_clause} AND jenis = 'Pengeluaran' GROUP BY kategori ORDER BY SUM(nominal) DESC",
            params
        )
        breakdown_rows = cursor.fetchall()

    saldo = total_masuk - total_keluar
    return total_masuk, total_keluar, saldo, judul, breakdown_rows

def get_kategori_breakdown(user_id: int, jenis: str = "Pengeluaran", period: str = "this_month", custom_ym: str = None):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        if period == "today":
            judul = f"Hari Ini ({datetime.now().strftime('%d-%m-%Y')})"
            where_clause = "user_id = ? AND strftime('%Y-%m-%d', tanggal) = strftime('%Y-%m-%d', 'now', 'localtime')"
            params = (user_id,)
        elif period == "this_month":
            judul = f"Bulan Ini ({datetime.now().strftime('%m-%Y')})"
            where_clause = "user_id = ? AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')"
            params = (user_id,)
        elif period == "all":
            judul = "Semua Waktu"
            where_clause = "user_id = ?"
            params = (user_id,)
        elif period == "custom" and custom_ym:
            judul = f"Periode {custom_ym}"
            where_clause = "user_id = ? AND strftime('%Y-%m', tanggal) = ?"
            params = (user_id, custom_ym)
        else:
            judul = "Bulan Ini"
            where_clause = "user_id = ? AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')"
            params = (user_id,)

        cursor.execute(
            f"SELECT kategori, SUM(nominal) FROM transaksi WHERE {where_clause} AND jenis = ? GROUP BY kategori ORDER BY SUM(nominal) DESC",
            params + (jenis,)
        )
        rows = cursor.fetchall()

        cursor.execute(f"SELECT SUM(nominal) FROM transaksi WHERE {where_clause} AND jenis = ?", params + (jenis,))
        total = cursor.fetchone()[0] or 0.0

    return rows, total, judul

async def rekap(update: Update, context: ContextTypes.DEFAULT_TYPE, period="this_month", custom_ym=None):
    if not await is_authorized(update):
        return

    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    total_masuk, total_keluar, saldo, judul, breakdown = get_rekap_data(user_id, period, custom_ym)
    saldo_emoji = "💰" if saldo >= 0 else "⚠️"

    pesan_rekap = (
        f"📊 <b>Ringkasan Keuangan ({judul})</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 <b>Total Pemasukan:</b> {format_rupiah(total_masuk)}\n"
        f"📤 <b>Total Pengeluaran:</b> {format_rupiah(total_keluar)}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{saldo_emoji} <b>Saldo Periode Ini:</b> <code>{format_rupiah(saldo)}</code>\n\n"
    )

    if breakdown:
        pesan_rekap += "🏷️ <b>Rincian Pengeluaran per Kategori:</b>\n"
        for cat, amount in breakdown:
            persen = (amount / total_keluar * 100) if total_keluar > 0 else 0
            pesan_rekap += f"• {cat}: <b>{format_rupiah(amount)}</b> (<i>{persen:.1f}%</i>)\n"
        pesan_rekap += "\n"

    pesan_rekap += "👇 <i>Pilih filter rentang waktu di bawah ini:</i>"

    if update.callback_query:
        await update.callback_query.edit_message_text(
            pesan_rekap,
            parse_mode=ParseMode.HTML,
            reply_markup=get_rekap_keyboard()
        )
    else:
        await update.message.reply_text(
            pesan_rekap,
            parse_mode=ParseMode.HTML,
            reply_markup=get_rekap_keyboard()
        )

# --- FITUR GRAFIK VISUAL LENGKAP (PEMASUKAN, PENGELUARAN, CASHFLOW) ---
def get_chart_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📤 Grafik Pengeluaran", callback_data="do_chart_pengeluaran")],
        [InlineKeyboardButton("📥 Grafik Pemasukan", callback_data="do_chart_pemasukan")],
        [InlineKeyboardButton("📊 Perbandingan Cashflow", callback_data="do_chart_cashflow")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def prompt_grafik_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    pesan = (
        "📈 <b>Pilihan Grafik Visual Keuangan</b>\n\n"
        "Silakan pilih jenis grafik visual yang ingin Anda tampilkan:"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(pesan, parse_mode=ParseMode.HTML, reply_markup=get_chart_menu_keyboard())
    else:
        await update.message.reply_text(pesan, parse_mode=ParseMode.HTML, reply_markup=get_chart_menu_keyboard())

# Generator Pie Chart Kategori
def generate_pie_chart(user_id: int, jenis: str = "Pengeluaran", period: str = "this_month", custom_ym: str = None):
    rows, total, judul = get_kategori_breakdown(user_id, jenis, period, custom_ym)
    
    if not rows or total <= 0:
        return None, judul

    categories = [strip_emoji(r[0]) for r in rows]
    amounts = [r[1] for r in rows]

    colors = ["#ff9999", "#66b3ff", "#99ff99", "#ffcc99", "#c2c2f0", "#ffb3e6", "#c4e17f", "#76D7C4", "#F7DC6F"]

    fig, ax = plt.subplots(figsize=(6, 6), dpi=120)
    wedges, texts, autotexts = ax.pie(
        amounts,
        labels=categories,
        autopct="%1.1f%%",
        startangle=140,
        colors=colors[:len(categories)],
        textprops=dict(color="black", weight="bold")
    )
    
    for autotext in autotexts:
        autotext.set_color("white")
        autotext.set_weight("bold")

    ax.set_title(f"Visualisasi {jenis}\n({judul})", fontsize=14, pad=15, weight="bold")
    plt.tight_layout()

    filename = f"grafik_{jenis.lower()}_{user_id}.png"
    plt.savefig(filename)
    plt.close(fig)
    return filename, judul

# Generator Bar Chart Cashflow (Pemasukan vs Pengeluaran)
def generate_cashflow_chart(user_id: int, period: str = "this_month", custom_ym: str = None):
    total_masuk, total_keluar, _, judul, _ = get_rekap_data(user_id, period, custom_ym)

    if total_masuk <= 0 and total_keluar <= 0:
        return None, judul

    fig, ax = plt.subplots(figsize=(6, 5), dpi=120)
    categories = ["Pemasukan", "Pengeluaran"]
    values = [total_masuk, total_keluar]
    colors = ["#2ecc71", "#e74c3c"]

    bars = ax.bar(categories, values, color=colors, width=0.45)

    for bar in bars:
        height = bar.get_height()
        ax.annotate(
            format_rupiah(height),
            xy=(bar.get_x() + bar.get_width() / 2, height),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center", va="bottom", fontweight="bold", fontsize=9
        )

    ax.set_ylabel("Nominal (Rp)", fontweight="bold")
    ax.set_title(f"Perbandingan Cashflow\n({judul})", fontsize=13, pad=15, fontweight="bold")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()

    filename = f"grafik_cashflow_{user_id}.png"
    plt.savefig(filename)
    plt.close(fig)
    return filename, judul

async def kirim_grafik_pie(update: Update, context: ContextTypes.DEFAULT_TYPE, jenis: str = "Pengeluaran", period="this_month"):
    if not await is_authorized(update):
        return

    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    filename, judul = generate_pie_chart(user_id, jenis, period)

    if not filename:
        msg = f"📊 Belum ada data {jenis.lower()} pada periode {judul} untuk dibuatkan grafik."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    caption = f"📈 <b>Grafik Visual {jenis} ({judul})</b>\n\nGrafik di atas menampilkan persentase alokasi {jenis.lower()} Anda per kategori."

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_photo(
            photo=open(filename, "rb"),
            caption=caption,
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_photo(
            photo=open(filename, "rb"),
            caption=caption,
            parse_mode=ParseMode.HTML
        )

    if os.path.exists(filename):
        os.remove(filename)

async def kirim_grafik_cashflow(update: Update, context: ContextTypes.DEFAULT_TYPE, period="this_month"):
    if not await is_authorized(update):
        return

    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    filename, judul = generate_cashflow_chart(user_id, period)

    if not filename:
        msg = f"📊 Belum ada data transaksi pada periode {judul} untuk dibuatkan grafik cashflow."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    caption = f"📊 <b>Grafik Perbandingan Cashflow ({judul})</b>\n\nDiagram batang membandingkan Total Uang Masuk vs Uang Keluar."

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_photo(
            photo=open(filename, "rb"),
            caption=caption,
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_photo(
            photo=open(filename, "rb"),
            caption=caption,
            parse_mode=ParseMode.HTML
        )

    if os.path.exists(filename):
        os.remove(filename)

# Custom Date Prompt Handler
async def prompt_custom_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.message.reply_text(
            "📆 <b>Filter Bulan & Tahun</b>\n\n"
            "Silakan kirim bulan dan tahun yang ingin dilihat.\n"
            "<i>Format:</i> <b>MM-YYYY</b> (Contoh: <code>07-2026</code> atau <code>12-2025</code>)\n\n"
            "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
    return CUSTOM_DATE_STATE

async def simpan_custom_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    match = re.match(r"^(\d{1,2})[-/](\d{4})$", text)
    if not match:
        await update.message.reply_text(
            "❌ Format salah! Harap kirimkan format <b>MM-YYYY</b> (Contoh: <code>07-2026</code> atau <code>12-2025</code>).\n"
            "Silakan coba lagi:",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
        return CUSTOM_DATE_STATE

    bln, thn = int(match.group(1)), match.group(2)
    if bln < 1 or bln > 12:
        await update.message.reply_text("❌ Bulan harus antara 01 sampai 12. Silakan coba lagi:")
        return CUSTOM_DATE_STATE

    custom_ym = f"{thn}-{bln:02d}"
    await rekap(update, context, period="custom", custom_ym=custom_ym)
    return ConversationHandler.END

# --- FITUR SET BUDGET ---
async def prompt_set_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT budget_bulanan FROM settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        current_budget = row[0] if row and row[0] else 0.0

    await update.message.reply_text(
        f"🎯 <b>Pengaturan Budget Bulanan</b>\n\n"
        f"Budget Bulanan Saat Ini: <b>{format_rupiah(current_budget)}</b>\n\n"
        "Silakan masukkan nominal budget bulanan baru Anda.\n"
        "<i>Contoh:</i> <code>3m</code>, <code>3.000.000</code>, atau kirim <code>0</code> untuk menghapus budget.\n\n"
        "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    return SET_BUDGET_STATE

async def simpan_set_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    try:
        nominal = parse_nominal(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Format angka tidak valid. Silakan coba masukkan nominal lagi (misal: <code>3.000.000</code>):",
            reply_markup=get_main_keyboard()
        )
        return SET_BUDGET_STATE

    user_id = update.effective_user.id
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (user_id, budget_bulanan) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET budget_bulanan = ?",
            (user_id, nominal, nominal)
        )
        conn.commit()

    if nominal > 0:
        msg = f"✅ Budget bulanan Anda berhasil diatur ke <b>{format_rupiah(nominal)}</b>!"
    else:
        msg = "✅ Budget bulanan telah dinonaktifkan."

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
    return ConversationHandler.END

# --- FITUR EDIT TRANSAKSI INTERAKTIF ---
async def prompt_edit_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        tx_id = int(query.data.split("_")[2])
        context.user_data["edit_tx_id"] = tx_id

    tx_id = context.user_data.get("edit_tx_id")
    await query.message.reply_text(
        f"💰 <b>Ubah Nominal Transaksi #{tx_id}</b>\n\n"
        "Silakan kirimkan nominal baru yang benar.\n"
        "<i>Contoh:</i> <code>1,5 juta</code>, <code>1.5jt</code>, <code>20k</code>, atau <code>25.000</code>\n\n"
        "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    return EDIT_NOMINAL_STATE

async def simpan_edit_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    try:
        nominal = parse_nominal(text)
        if nominal <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Nominal tidak valid. Silakan kirimkan nominal baru lagi:")
        return EDIT_NOMINAL_STATE

    tx_id = context.user_data.get("edit_tx_id")
    period = context.user_data.get("edit_period", "today")
    page = context.user_data.get("edit_page", 1)
    user_id = update.effective_user.id

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE transaksi SET nominal = ? WHERE id = ? AND user_id = ?", (nominal, tx_id, user_id))
        conn.commit()

    await update.message.reply_text(f"✅ Nominal transaksi #{tx_id} berhasil diubah ke <b>{format_rupiah(nominal)}</b>!", parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
    await riwayat(update, context, period=period, page=page)
    return ConversationHandler.END

async def prompt_edit_keterangan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        tx_id = int(query.data.split("_")[2])
        context.user_data["edit_tx_id"] = tx_id

    tx_id = context.user_data.get("edit_tx_id")
    await query.message.reply_text(
        f"📝 <b>Ubah Keterangan Transaksi #{tx_id}</b>\n\n"
        "Silakan kirimkan deskripsi / keterangan baru.\n"
        "<i>Contoh:</i> <code>Makan Siang Sate Ayam</code>\n\n"
        "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    return EDIT_KET_STATE

async def simpan_edit_keterangan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    tx_id = context.user_data.get("edit_tx_id")
    period = context.user_data.get("edit_period", "today")
    page = context.user_data.get("edit_page", 1)
    user_id = update.effective_user.id

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE transaksi SET keterangan = ? WHERE id = ? AND user_id = ?", (text, tx_id, user_id))
        conn.commit()

    await update.message.reply_text(f"✅ Keterangan transaksi #{tx_id} berhasil diperbarui!", parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
    await riwayat(update, context, period=period, page=page)
    return ConversationHandler.END

def get_admin_stats():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'active'")
        active_users = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'blocked'")
        blocked_users = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM transaksi")
        total_tx = cursor.fetchone()[0] or 0
        cursor.execute(
            "SELECT COUNT(*) FROM transaksi WHERE strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')"
        )
        month_tx = cursor.fetchone()[0] or 0
    return total_users, active_users, blocked_users, total_tx, month_tx

def get_users_for_admin(limit: int = 20):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT u.user_id, u.username, u.first_name, u.role, u.status, COUNT(t.id) AS tx_count
            FROM users u
            LEFT JOIN transaksi t ON t.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY u.last_seen_at DESC, u.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cursor.fetchall()

def format_admin_user_label(user_id, username, first_name, role, status, tx_count) -> str:
    name = f"@{username}" if username else (first_name or str(user_id))
    return f"{name} | {role} | {status} | {tx_count} tx"

async def main_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_authorized(update):
        return

    total_users, active_users, blocked_users, total_tx, month_tx = get_admin_stats()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Daftar User", callback_data="mainadmin_users")],
        [InlineKeyboardButton("📊 Statistik Global", callback_data="mainadmin_stats")],
    ])
    pesan = (
        "🛡️ <b>Admin Utama</b>\n\n"
        f"👥 User: <b>{total_users}</b> total, <b>{active_users}</b> aktif, <b>{blocked_users}</b> diblokir\n"
        f"📌 Transaksi: <b>{total_tx}</b> total, <b>{month_tx}</b> bulan ini\n\n"
        "Gunakan panel ini untuk memantau user dan mengatur akses."
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def admin_users_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_authorized(update):
        return

    query = update.callback_query
    rows = get_users_for_admin()
    if not rows:
        msg = "👥 <b>Daftar User</b>\n\nBelum ada user terdaftar."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="mainadmin_back")]])
    else:
        msg = "👥 <b>Daftar User Terbaru</b>\n\n"
        buttons = []
        for user_id, username, first_name, role, status, tx_count in rows:
            msg += f"• <code>{user_id}</code> - {format_admin_user_label(user_id, username, first_name, role, status, tx_count)}\n"
            if role != "admin":
                action = "unblockuser" if status == "blocked" else "blockuser"
                label = "✅ Aktifkan" if status == "blocked" else "🚫 Blokir"
                buttons.append([InlineKeyboardButton(f"{label} {username or first_name or user_id}", callback_data=f"{action}_{user_id}")])
        buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="mainadmin_back")])
        keyboard = InlineKeyboardMarkup(buttons)

    if query:
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def admin_stats_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_authorized(update):
        return

    total_users, active_users, blocked_users, total_tx, month_tx = get_admin_stats()
    msg = (
        "📊 <b>Statistik Global</b>\n\n"
        f"👥 Total user: <b>{total_users}</b>\n"
        f"✅ User aktif: <b>{active_users}</b>\n"
        f"🚫 User diblokir: <b>{blocked_users}</b>\n"
        f"📌 Total transaksi: <b>{total_tx}</b>\n"
        f"🗓️ Transaksi bulan ini: <b>{month_tx}</b>"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="mainadmin_back")]])

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def set_user_status(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, status: str):
    if not await is_admin_authorized(update):
        return
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET status = ? WHERE user_id = ? AND role != 'admin'", (status, target_user_id))
        conn.commit()
    if update.callback_query:
        await update.callback_query.answer(f"Status user {target_user_id} diubah menjadi {status}.", show_alert=True)
        await admin_users_panel(update, context)
    else:
        await update.message.reply_text(f"✅ Status user <code>{target_user_id}</code> diubah menjadi <b>{status}</b>.", parse_mode=ParseMode.HTML)

async def block_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /block <user_id>")
        return
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID harus berupa angka. Format: /block <user_id>")
        return
    await set_user_status(update, context, target_user_id, "blocked")

async def unblock_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /unblock <user_id>")
        return
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID harus berupa angka. Format: /unblock <user_id>")
        return
    await set_user_status(update, context, target_user_id, "active")

# --- FITUR ADMIN / KELOLA KATEGORI ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Kategori Kustom", callback_data="admin_add_cat")],
        [InlineKeyboardButton("📋 Lihat Semua Kategori", callback_data="admin_list_cat")],
        [InlineKeyboardButton("🗑️ Hapus Kategori Kustom", callback_data="admin_del_cat_list")]
    ])

    pesan = (
        "⚙️ <b>Pengaturan Saya</b>\n\n"
        "Anda dapat menambah atau menghapus kategori transaksi Anda sendiri tanpa perlu menyentuh kodingan!"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def handle_add_cat_jenis_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    query = update.callback_query
    await query.answer()
    data = query.data
    jenis = data.split("_")[1]
    context.user_data["admin_cat_jenis"] = jenis

    await query.message.reply_text(
        f"➕ <b>Tambah Kategori {jenis} Baru</b>\n\n"
        "Silakan kirimkan nama kategori baru yang ingin Anda buat.\n"
        "<i>Contoh:</i> <code>🎮 Gaming</code> atau <code>🎓 Biaya Kuliah</code>\n\n"
        "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    return ADD_CAT_NAME_STATE

async def simpan_custom_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    jenis = context.user_data.get("admin_cat_jenis", "Pengeluaran")
    user_id = update.effective_user.id

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO custom_kategori (user_id, jenis, nama_kategori) VALUES (?, ?, ?)",
            (user_id, jenis, text)
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ Kategori <b>{text}</b> ({jenis}) berhasil ditambahkan!",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

# --- FITUR EXPORT LAPORAN (CSV & PDF) ---
async def prompt_export_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Export CSV (Excel)", callback_data="do_export_csv")],
        [InlineKeyboardButton("📄 Export Dokumen PDF", callback_data="do_export_pdf")]
    ])
    pesan = (
        "📑 <b>Export Laporan Keuangan</b>\n\n"
        "Silakan pilih format laporan yang ingin Anda unduh:"
    )
    await update.message.reply_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, jenis, nominal, kategori, keterangan, tanggal FROM transaksi WHERE user_id = ? ORDER BY id ASC",
            (user_id,)
        )
        rows = cursor.fetchall()

    if not rows:
        msg = "📊 Belum ada data transaksi untuk di-export."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    filename = f"laporan_keuangan_{user_id}.csv"
    with open(filename, mode="w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["ID", "Jenis", "Nominal", "Kategori", "Keterangan", "Tanggal"])
        for row in rows:
            writer.writerow(row)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_document(
            document=open(filename, "rb"),
            filename="Laporan_Keuangan_Pribadi.csv",
            caption="📊 <b>Laporan Keuangan Anda (CSV/Excel)</b>",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_document(
            document=open(filename, "rb"),
            filename="Laporan_Keuangan_Pribadi.csv",
            caption="📊 <b>Laporan Keuangan Anda (CSV/Excel)</b>",
            parse_mode=ParseMode.HTML
        )

    if os.path.exists(filename):
        os.remove(filename)

async def export_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, jenis, nominal, kategori, keterangan, tanggal FROM transaksi WHERE user_id = ? ORDER BY id ASC",
            (user_id,)
        )
        rows = cursor.fetchall()

        cursor.execute("SELECT SUM(nominal) FROM transaksi WHERE user_id = ? AND jenis = 'Pemasukan'", (user_id,))
        tot_masuk = cursor.fetchone()[0] or 0.0

        cursor.execute("SELECT SUM(nominal) FROM transaksi WHERE user_id = ? AND jenis = 'Pengeluaran'", (user_id,))
        tot_keluar = cursor.fetchone()[0] or 0.0

    if not rows:
        msg = "📄 Belum ada data transaksi untuk di-export ke PDF."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    if update.callback_query:
        await update.callback_query.answer()

    saldo = tot_masuk - tot_keluar

    pdf = PDFReport(orientation="P", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.add_page()

    # Box Ringkasan Keuangan
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(0, 8, "  Ringkasan Keuangan", border=1, new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(63, 7, f"Total Pemasukan: {format_rupiah(tot_masuk)}", border=1, align="C")
    pdf.cell(63, 7, f"Total Pengeluaran: {format_rupiah(tot_keluar)}", border=1, align="C")
    pdf.cell(64, 7, f"Saldo Akhir: {format_rupiah(saldo)}", border=1, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(6)

    # Header Tabel Transaksi
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(220, 230, 245)
    pdf.cell(10, 8, "No.", border=1, align="C", fill=True)
    pdf.cell(12, 8, "ID", border=1, align="C", fill=True)
    pdf.cell(32, 8, "Waktu", border=1, align="C", fill=True)
    pdf.cell(25, 8, "Jenis", border=1, align="C", fill=True)
    pdf.cell(30, 8, "Kategori", border=1, align="C", fill=True)
    pdf.cell(43, 8, "Keterangan", border=1, align="C", fill=True)
    pdf.cell(38, 8, "Nominal", border=1, new_x="LMARGIN", new_y="NEXT", align="C", fill=True)

    pdf.set_font("Helvetica", "", 8)
    for idx, (tx_id, jenis, nominal, kat, ket, tgl) in enumerate(rows, start=1):
        ket_clean = str(ket).encode("latin-1", "replace").decode("latin-1")
        kat_clean = str(kat).encode("latin-1", "replace").decode("latin-1")
        
        pdf.cell(10, 7, str(idx), border=1, align="C")
        pdf.cell(12, 7, f"#{tx_id}", border=1, align="C")
        pdf.cell(32, 7, str(tgl), border=1, align="C")
        pdf.cell(25, 7, str(jenis), border=1, align="C")
        pdf.cell(30, 7, kat_clean[:18], border=1)
        pdf.cell(43, 7, ket_clean[:25], border=1)
        pdf.cell(38, 7, format_rupiah(nominal), border=1, new_x="LMARGIN", new_y="NEXT", align="R")

    filename = f"laporan_keuangan_{user_id}.pdf"
    pdf.output(filename)

    if update.callback_query:
        await update.callback_query.message.reply_document(
            document=open(filename, "rb"),
            filename="Laporan_Keuangan_Pribadi.pdf",
            caption="📄 <b>Laporan Keuangan Anda (Format PDF)</b>",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_document(
            document=open(filename, "rb"),
            filename="Laporan_Keuangan_Pribadi.pdf",
            caption="📄 <b>Laporan Keuangan Anda (Format PDF)</b>",
            parse_mode=ParseMode.HTML
        )

    if os.path.exists(filename):
        os.remove(filename)

async def backup_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_authorized(update):
        return

    if not os.path.exists(DB_NAME):
        await update.effective_message.reply_text("❌ File database belum ditemukan.")
        return

    await update.effective_message.reply_document(
        document=open(DB_NAME, "rb"),
        filename="keuangan_backup.db",
        caption="📦 <b>Backup Database Keuangan</b>\n\nFile ini berisi data semua user. Simpan file ini dengan aman!",
        parse_mode=ParseMode.HTML
    )

# --- FITUR RIWAYAT PAGINATION & BUTTON EDIT/HAPUS ---
async def riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE, period="today", page=1):
    if not await is_authorized(update):
        return

    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        
        if period == "today":
            judul_periode = f"Hari Ini ({datetime.now().strftime('%d-%m-%Y')})"
            where_sql = "WHERE user_id = ? AND strftime('%Y-%m-%d', tanggal) = strftime('%Y-%m-%d', 'now', 'localtime')"
            params = (user_id,)
        elif period == "this_month":
            judul_periode = f"Bulan Ini ({datetime.now().strftime('%m-%Y')})"
            where_sql = "WHERE user_id = ? AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')"
            params = (user_id,)
        else:
            judul_periode = "Semua Waktu"
            where_sql = "WHERE user_id = ?"
            params = (user_id,)

        cursor.execute(f"SELECT COUNT(*) FROM transaksi {where_sql}", params)
        total_count = cursor.fetchone()[0] or 0
        total_pages = max(1, math.ceil(total_count / PER_PAGE))

        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages

        offset = (page - 1) * PER_PAGE

        cursor.execute(
            f"SELECT id, jenis, nominal, keterangan, tanggal, kategori FROM transaksi {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + (PER_PAGE, offset)
        )
        rows = cursor.fetchall()

    inline_keyboard = []

    if not rows:
        pesan = f"📋 <b>Riwayat Transaksi ({judul_periode})</b>\n\n<i>Belum ada transaksi pada periode ini.</i>"
    else:
        pesan = f"📋 <b>Riwayat Transaksi ({judul_periode})</b> - Halaman {page}/{total_pages}:\n\n"
        for item in rows:
            tx_id, jenis, nominal, ket, tgl, kat = item
            icon = "🔴" if jenis == "Pengeluaran" else "🟢"
            pesan += f"{icon} <b>#{tx_id}</b> [{jenis}] {format_rupiah(nominal)}\n"
            pesan += f"   🏷️ <i>{kat}</i> | {ket} ({tgl})\n\n"
            
            inline_keyboard.append([
                InlineKeyboardButton(f"✏️ Edit #{tx_id}", callback_data=f"editopt_{tx_id}_{period}_{page}"),
                InlineKeyboardButton(f"🗑️ Hapus #{tx_id}", callback_data=f"askdel_{tx_id}_{period}_{page}")
            ])

    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀️ Sebelum", callback_data=f"rw_{period}_{page-1}"))
        else:
            nav_row.append(InlineKeyboardButton(" 💬 ", callback_data="noop"))

        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))

        if page < total_pages:
            nav_row.append(InlineKeyboardButton("Sesudah ▶️", callback_data=f"rw_{period}_{page+1}"))
        else:
            nav_row.append(InlineKeyboardButton(" 💬 ", callback_data="noop"))

        inline_keyboard.append(nav_row)

    filter_row = [
        InlineKeyboardButton("📅 Hari Ini", callback_data="rw_today_1"),
        InlineKeyboardButton("🗓️ Bulan Ini", callback_data="rw_this_month_1"),
        InlineKeyboardButton("♾️ Semua", callback_data="rw_all_1"),
    ]
    inline_keyboard.append(filter_row)

    reply_markup = InlineKeyboardMarkup(inline_keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            pesan,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            pesan,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )

# Callback Handler Umum
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == "noop":
        await query.answer()
        return

    elif data == "mainadmin_back":
        await query.answer()
        await main_admin_panel(update, context)
    elif data == "mainadmin_users":
        await query.answer()
        await admin_users_panel(update, context)
    elif data == "mainadmin_stats":
        await query.answer()
        await admin_stats_panel(update, context)
    elif data.startswith("blockuser_"):
        target_user_id = int(data.split("_")[1])
        await set_user_status(update, context, target_user_id, "blocked")
    elif data.startswith("unblockuser_"):
        target_user_id = int(data.split("_")[1])
        await set_user_status(update, context, target_user_id, "active")

    # Export Handlers
    elif data == "do_export_csv":
        await export_csv(update, context)
    elif data == "do_export_pdf":
        await export_pdf(update, context)

    # Show Grafik Handlers
    elif data == "chart_menu_prompt":
        await prompt_grafik_menu(update, context)
    elif data == "do_chart_pengeluaran":
        await kirim_grafik_pie(update, context, jenis="Pengeluaran")
    elif data == "do_chart_pemasukan":
        await kirim_grafik_pie(update, context, jenis="Pemasukan")
    elif data == "do_chart_cashflow":
        await kirim_grafik_cashflow(update, context)

    # Filter Rekap
    elif data == "rekap_today":
        await query.answer()
        await rekap(update, context, period="today")
    elif data == "rekap_this_month":
        await query.answer()
        await rekap(update, context, period="this_month")
    elif data == "rekap_all":
        await query.answer()
        await rekap(update, context, period="all")

    # Filter Riwayat & Pagination
    elif data.startswith("rw_"):
        await query.answer()
        parts = data.split("_")
        period = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 1
        await riwayat(update, context, period=period, page=page)

    # Menu Opsi Edit Transaksi (#tx_id)
    elif data.startswith("editopt_"):
        await query.answer()
        parts = data.split("_")
        tx_id = int(parts[1])
        period = parts[2]
        page = int(parts[3])

        context.user_data["edit_tx_id"] = tx_id
        context.user_data["edit_period"] = period
        context.user_data["edit_page"] = page

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT jenis, nominal, keterangan, kategori FROM transaksi WHERE id = ? AND user_id = ?", (tx_id, user_id))
            row = cursor.fetchone()

        if not row:
            await query.answer("❌ Transaksi tidak ditemukan.", show_alert=True)
            return

        jenis, nominal, ket, kat = row
        text_menu = (
            f"✏️ <b>Edit Transaksi #{tx_id}</b>\n\n"
            f"📌 <b>Jenis:</b> {jenis}\n"
            f"💰 <b>Nominal:</b> {format_rupiah(nominal)}\n"
            f"🏷️ <b>Kategori:</b> {kat}\n"
            f"📝 <b>Keterangan:</b> {ket}\n\n"
            "Silakan pilih bagian yang ingin diubah:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Edit Nominal", callback_data=f"doedit_nom_{tx_id}")],
            [InlineKeyboardButton("🏷️ Edit Kategori", callback_data=f"doedit_catmenu_{tx_id}")],
            [InlineKeyboardButton("📝 Edit Keterangan", callback_data=f"doedit_ket_{tx_id}")],
            [InlineKeyboardButton("🔙 Kembali ke Riwayat", callback_data=f"rw_{period}_{page}")]
        ])
        await query.edit_message_text(text_menu, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    # Ganti Kategori Langsung dari Tombol Inline
    elif data.startswith("setcat_"):
        await query.answer()
        parts = data.split("_")
        tx_id = int(parts[1])
        cat_idx = int(parts[2])

        period = context.user_data.get("edit_period", "today")
        page = context.user_data.get("edit_page", 1)

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT jenis FROM transaksi WHERE id = ? AND user_id = ?", (tx_id, user_id))
            row = cursor.fetchone()
            if row:
                jenis = row[0]
                cats = get_user_categories(user_id, jenis)
                new_cat = cats[cat_idx] if cat_idx < len(cats) else "Umum"

                cursor.execute("UPDATE transaksi SET kategori = ? WHERE id = ? AND user_id = ?", (new_cat, tx_id, user_id))
                conn.commit()

        await query.answer(f"✅ Kategori diubah ke {new_cat}!", show_alert=True)
        await riwayat(update, context, period=period, page=page)

    # Menu Pilih Kategori Baru untuk Transaksi #tx_id
    elif data.startswith("doedit_catmenu_"):
        await query.answer()
        tx_id = int(data.split("_")[2])
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT jenis FROM transaksi WHERE id = ? AND user_id = ?", (tx_id, user_id))
            row = cursor.fetchone()

        if row:
            jenis = row[0]
            cats = get_user_categories(user_id, jenis)
            buttons = []
            row_btn = []
            for i, c in enumerate(cats):
                row_btn.append(InlineKeyboardButton(c, callback_data=f"setcat_{tx_id}_{i}"))
                if len(row_btn) == 2:
                    buttons.append(row_btn)
                    row_btn = []
            if row_btn:
                buttons.append(row_btn)
            
            period = context.user_data.get("edit_period", "today")
            page = context.user_data.get("edit_page", 1)
            buttons.append([InlineKeyboardButton("🔙 Batal", callback_data=f"rw_{period}_{page}")])

            await query.edit_message_text(
                f"🏷️ <b>Pilih Kategori Baru untuk Transaksi #{tx_id}:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    # Konfirmasi Hapus Transaksi
    elif data.startswith("askdel_"):
        await query.answer()
        parts = data.split("_")
        tx_id = int(parts[1])
        period = parts[2] if len(parts) > 2 else "today"
        page = int(parts[3]) if len(parts) > 3 else 1

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT jenis, nominal, keterangan, tanggal, kategori FROM transaksi WHERE id = ? AND user_id = ?", (tx_id, user_id))
            row = cursor.fetchone()

        if not row:
            await query.answer("❌ Transaksi tidak ditemukan.", show_alert=True)
            await riwayat(update, context, period=period, page=page)
            return

        jenis, nominal, ket, tgl, kat = row
        confirm_text = (
            f"⚠️ <b>Konfirmasi Hapus Transaksi #{tx_id}</b>\n\n"
            f"📌 <b>Jenis:</b> {jenis}\n"
            f"🏷️ <b>Kategori:</b> {kat}\n"
            f"💰 <b>Nominal:</b> {format_rupiah(nominal)}\n"
            f"📝 <b>Keterangan:</b> {ket}\n"
            f"📅 <b>Waktu:</b> {tgl}\n\n"
            "<b>Apakah Anda yakin ingin menghapus transaksi ini?</b>"
        )
        confirm_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Ya, Hapus", callback_data=f"confirmdel_{tx_id}_{period}_{page}"),
                InlineKeyboardButton("❌ Batal", callback_data=f"rw_{period}_{page}")
            ]
        ])
        await query.edit_message_text(confirm_text, parse_mode=ParseMode.HTML, reply_markup=confirm_keyboard)

    # Eksekusi Hapus Transaksi
    elif data.startswith("confirmdel_"):
        parts = data.split("_")
        tx_id = int(parts[1])
        period = parts[2] if len(parts) > 2 else "today"
        page = int(parts[3]) if len(parts) > 3 else 1

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM transaksi WHERE id = ? AND user_id = ?", (tx_id, user_id))
            conn.commit()

        await query.answer(f"✅ Transaksi #{tx_id} berhasil dihapus!", show_alert=True)
        await riwayat(update, context, period=period, page=page)

    # --- CALLBACK ADMIN PANEL ---
    elif data == "admin_add_cat":
        await query.answer()
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Kategori Pemasukan", callback_data="addcatjenis_Pemasukan"),
                InlineKeyboardButton("📤 Kategori Pengeluaran", callback_data="addcatjenis_Pengeluaran")
            ]
        ])
        await query.edit_message_text("➕ Pilih jenis kategori yang ingin ditambahkan:", reply_markup=keyboard)

    elif data == "admin_list_cat":
        await query.answer()
        cats_masuk = get_user_categories(user_id, "Pemasukan")
        cats_keluar = get_user_categories(user_id, "Pengeluaran")

        msg = "📋 <b>Daftar Kategori Anda:</b>\n\n"
        msg += "<b>📥 Pemasukan:</b>\n" + "\n".join([f"• {c}" for c in cats_masuk]) + "\n\n"
        msg += "<b>📤 Pengeluaran:</b>\n" + "\n".join([f"• {c}" for c in cats_keluar])
        
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="admin_back")]])
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    elif data == "admin_del_cat_list":
        await query.answer()
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, jenis, nama_kategori FROM custom_kategori WHERE user_id = ?", (user_id,))
            rows = cursor.fetchall()

        if not rows:
            await query.answer("❌ Anda belum memiliki kategori kustom.", show_alert=True)
            return

        buttons = []
        for cid, jenis, nama in rows:
            buttons.append([InlineKeyboardButton(f"🗑️ Hapus {nama} ({jenis})", callback_data=f"delcatid_{cid}")])
        buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="admin_back")])

        await query.edit_message_text("🗑️ Pilih kategori kustom yang ingin dihapus:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("delcatid_"):
        cid = int(data.split("_")[1])
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM custom_kategori WHERE id = ? AND user_id = ?", (cid, user_id))
            conn.commit()

        await query.answer("✅ Kategori kustom berhasil dihapus!", show_alert=True)
        await admin_panel(update, context)

    elif data == "admin_back":
        await query.answer()
        await admin_panel(update, context)

# --- DAILY REMINDER JOB ---
async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE status = 'active'")
        user_ids = [row[0] for row in cursor.fetchall()]

    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "🔔 <b>Pengingat Catat Keuangan</b>\n\n"
                    "Halo! Sudahkah Anda mencatat pengeluaran & pemasukan Anda hari ini? 😉\n\n"
                    "Tekan <b>📥 Tambah Pemasukan</b> atau <b>📤 Tambah Pengeluaran</b> di bawah untuk mencatat."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logging.error(f"Gagal mengirim reminder ke {uid}: {e}")

if __name__ == "__main__":
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    # ConversationHandler Alur Transaksi, Custom Date, Budget, Edit & Admin
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📥 Tambah Pemasukan$"), prompt_pemasukan),
            MessageHandler(filters.Regex("^📤 Tambah Pengeluaran$"), prompt_pengeluaran),
            MessageHandler(filters.Regex("^🎯 Set Budget$"), prompt_set_budget),
            CommandHandler("masuk", prompt_pemasukan),
            CommandHandler("keluar", prompt_pengeluaran),
            CommandHandler("setbudget", prompt_set_budget),
            CallbackQueryHandler(prompt_custom_date, pattern="^rekap_custom_prompt$"),
            CallbackQueryHandler(prompt_edit_nominal, pattern="^doedit_nom_"),
            CallbackQueryHandler(prompt_edit_keterangan, pattern="^doedit_ket_"),
            CallbackQueryHandler(handle_add_cat_jenis_callback, pattern="^addcatjenis_"),
        ],
        states={
            KATEGORI_STATE: [
                CallbackQueryHandler(select_category_callback, pattern="^selectcat_"),
                CallbackQueryHandler(select_category_callback, pattern="^cancel_conv$"),
            ],
            NOMINAL_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_transaksi_input)],
            SET_BUDGET_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_set_budget)],
            CUSTOM_DATE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_custom_date)],
            EDIT_NOMINAL_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_edit_nominal)],
            EDIT_KET_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_edit_keterangan)],
            ADD_CAT_NAME_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_custom_category)],
        },
        fallbacks=[
            CommandHandler("batal", batal),
            MessageHandler(filters.Regex("^❌ Batal$"), batal),
        ],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bantuan", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("rekap", rekap))
    app.add_handler(CommandHandler("grafik", prompt_grafik_menu))
    app.add_handler(CommandHandler("riwayat", riwayat))
    app.add_handler(CommandHandler("export", prompt_export_menu))
    app.add_handler(CommandHandler("backup", backup_db))
    app.add_handler(CommandHandler("admin", main_admin_panel))
    app.add_handler(CommandHandler("users", admin_users_panel))
    app.add_handler(CommandHandler("stats", admin_stats_panel))
    app.add_handler(CommandHandler("block", block_user_command))
    app.add_handler(CommandHandler("unblock", unblock_user_command))
    
    # Text Message Handlers untuk Menu Keyboard Utama
    app.add_handler(MessageHandler(filters.Regex("^📊 Rekap Keuangan$"), rekap))
    app.add_handler(MessageHandler(filters.Regex("^📈 Grafik Visual$"), prompt_grafik_menu))
    app.add_handler(MessageHandler(filters.Regex("^📋 Riwayat Transaksi$"), riwayat))
    app.add_handler(MessageHandler(filters.Regex("^🎯 Set Budget$"), prompt_set_budget))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Admin & Kelola$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Pengaturan Saya$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex("^🛡️ Admin Utama$"), main_admin_panel))
    app.add_handler(MessageHandler(filters.Regex("^📑 Export Laporan$"), prompt_export_menu))
    app.add_handler(MessageHandler(filters.Regex("^📊 Export CSV$"), prompt_export_menu))
    app.add_handler(MessageHandler(filters.Regex("^📦 Backup DB$"), backup_db))
    app.add_handler(MessageHandler(filters.Regex("^❓ Bantuan$"), start))
    app.add_handler(MessageHandler(filters.Regex("^❌ Batal$"), batal))

    # Callback Query Handler Umum
    app.add_handler(CallbackQueryHandler(button_callback))

    # Jadwal Pengingat Harian (Setiap jam 20:00 WIB)
    if app.job_queue:
        app.job_queue.run_daily(send_daily_reminder, time=time(hour=20, minute=0, second=0))

    print("🚀 Bot Keuangan Multi-User sedang berjalan...")
    app.run_polling()
