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
    ADD_WALLET_NAME_STATE,
    SET_WALLET_SALDO_STATE,
    TRANSFER_WALLET_AMOUNT_STATE,
    EDIT_WALLET_NAME_STATE,
    INPUT_HP_NAME_STATE,
    INPUT_HP_NOMINAL_STATE,
) = range(1, 13)

# Token Bot Telegram & Authorization Whitelist
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID_RAW = os.getenv("ALLOWED_USER_ID", "").strip()


if not TOKEN:
    print("❌ ERROR: TELEGRAM_BOT_TOKEN tidak ditemukan di berkas .env! Silakan isi TELEGRAM_BOT_TOKEN Anda.")
    exit(1)

DB_NAME = "keuangan.db"
PER_PAGE = 5  # Item per halaman di riwayat

# Daftar Kategori Preset Default
DEFAULT_PENGELUARAN_KATEGORI = ["🍔 Makanan", "🚗 Transportasi", "🛍️ Belanja", "🏠 Tagihan", "🎬 Hiburan", "💊 Kesehatan", "📦 Lainnya"]
DEFAULT_PEMASUKAN_KATEGORI = ["💼 Gaji", "🎁 Bonus", "📈 Investasi", "💵 Usaha", "📦 Lainnya"]

# Middleware Pengecekan Otorisasi Pengguna
async def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_ID_RAW:
        return True

    user = update.effective_user
    if not user:
        return False

    target = ALLOWED_USER_ID_RAW.lstrip("@").lower()

    if user.username and user.username.lower() == target:
        return True

    if str(user.id) == target:
        return True

    msg = (
        "🔒 <b>Akses Ditolak!</b>\n\n"
        "Maaf, bot keuangan ini telah diproteksi khusus untuk penggunaan pribadi pemiliknya."
    )
    if update.callback_query:
        await update.callback_query.answer("🔒 Akses Ditolak!", show_alert=True)
    elif update.message:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    return False

# Helper Pembersih Emoji untuk Label Matplotlib
def strip_emoji(text: str) -> str:
    clean = re.sub(r'[^\w\s-]', '', text).strip()
    return clean if clean else text

# --- MATCH PERINTAH FLEKSIBEL (FUZZY & SYNONYM MATCHING) ---
def match_flexible_command_intent(text: str) -> str:
    raw = text.lower().strip()
    clean = re.sub(r'[^\w\s]', '', raw).strip()
    
    if any(k in clean for k in ["batal", "cancel", "stop", "abort"]):
        return "batal"
    if any(k in clean for k in ["rekap", "saldo", "summary", "sisa uang", "total saldo", "keuangan", "laporan bulanan"]):
        return "rekap"
    if any(k in clean for k in ["riwayat", "history", "catatan", "daftar", "list transaksi", "cek riwayat"]):
        return "riwayat"
    if any(k in clean for k in ["grafik", "chart", "diagram", "pie", "lihat grafik"]):
        return "grafik"
    if any(k in clean for k in ["budget", "anggaran", "target budget"]):
        return "budget"
    if any(k in clean for k in ["export", "unduh", "download", "pdf", "excel", "csv"]):
        return "export"
    if any(k in clean for k in ["backup", "cadangan", "database"]):
        return "backup"
    if any(k in clean for k in ["dompet", "wallet", "saldo dompet", "rekening"]):
        return "dompet"
    if any(k in clean for k in ["hutang", "piutang", "utang", "pinjaman"]):
        return "hutang"
    if any(k in clean for k in ["help", "bantuan", "panduan", "cara pakai", "bantu", "tanya"]):
        return "bantuan"
    if any(k in clean for k in ["tambah pemasukan", "tambah masuk", "catat pemasukan"]):
        return "prompt_pemasukan"
    if any(k in clean for k in ["tambah pengeluaran", "tambah keluar", "catat pengeluaran"]):
        return "prompt_pengeluaran"

    return None

# --- SMART NLP INTENT & CATEGORY CLASSIFIER ---
def extract_nominal_from_sentence(text: str):
    pattern = r"(?:\b|(?<=\s))(\d+(?:[.,]\d+)?)\s*(k|rb|ribu|jt|juta|m|miliar)?(?:\b|(?=\s))"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None

    raw_num = match.group(1)
    unit = (match.group(2) or "").lower()
    
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
    elif "," in raw_num:
        raw_num = raw_num.replace(",", ".")

    try:
        nominal = float(raw_num) * multiplier
    except ValueError:
        return None

    if nominal <= 0:
        return None

    keterangan = (text[:match.start()] + text[match.end():]).strip()
    keterangan = re.sub(r'\s+', ' ', keterangan).strip()
    if not keterangan:
        keterangan = text.strip()

    return nominal, keterangan, text.strip()

def detect_jenis_and_kategori(full_text: str, user_id: int):
    low = full_text.lower()

    pemasukan_keywords = [
        "dapat", "dapet", "gaji", "gajian", "terima", "diberi", "dikasih", 
        "bonus", "transferan", "uang masuk", "sangu", "omset", "penjualan", 
        "cashback", "cair", "dapat dari", "diberikan", "hadiah", "dapat saku",
        "pemberian", "thr", "proyek", "freelance", "diisi ibu", "diberi ibu",
        "dikasih ibu", "diberi ayah", "dikasih ayah", "masuk"
    ]

    is_pemasukan = any(kw in low for kw in pemasukan_keywords)
    jenis = "Pemasukan" if is_pemasukan else "Pengeluaran"

    clean_low = re.sub(r'[^\w\s]', '', low)

    if jenis == "Pemasukan":
        if any(k in clean_low for k in ["gaji", "gajian", "proyek", "freelance"]):
            kategori = "💼 Gaji"
        elif any(k in clean_low for k in ["bonus", "thr", "cashback", "omset"]):
            kategori = "🎁 Bonus"
        elif any(k in clean_low for k in ["investasi", "saham", "crypto"]):
            kategori = "📈 Investasi"
        elif any(k in clean_low for k in ["sangu", "ibu", "ayah", "dikasih", "diberi", "hadiah", "saku"]):
            kategori = "🎁 Bonus"
        else:
            kategori = "💼 Gaji"
    else:
        if any(k in clean_low for k in ["makan", "minum", "ayam", "nasi", "kopi", "ngopi", "sate", "bakso", "teh", "snack", "roti", "pizza", "gorengan", "jus", "susu", "martabak", "geprek", "lunch", "dinner", "sarapan", "food", "resto", "warung"]):
            kategori = "🍔 Makanan"
        elif any(k in clean_low for k in ["bensin", "parkir", "grab", "gojek", "gocar", "goride", "ojek", "mrt", "krl", "tol", "pertamax", "pertalite", "karcis", "servis", "tambal", "ban"]):
            kategori = "🚗 Transportasi"
        elif any(k in clean_low for k in ["beli", "shopee", "tokped", "tokopedia", "baju", "celana", "sepatu", "indomaret", "alfamart", "skincare", "makeup", "tas"]):
            kategori = "🛍️ Belanja"
        elif any(k in clean_low for k in ["listrik", "air", "wifi", "indihome", "pdam", "pulsa", "kuota", "sewa", "kost", "token", "tagihan", "netflix", "spotify", "isix", "isi"]):
            kategori = "🏠 Tagihan"
        elif any(k in clean_low for k in ["nonton", "bioskop", "game", "steam", "topup", "hiburan", "liburan", "tiket"]):
            kategori = "🎬 Hiburan"
        elif any(k in clean_low for k in ["obat", "dokter", "apotek", "vitamin", "sakit", "rs", "klinik", "sehat"]):
            kategori = "💊 Kesehatan"
        else:
            kategori = "📦 Lainnya"

    user_cats = get_user_categories(user_id, jenis)
    for c in user_cats:
        c_clean = strip_emoji(c).lower().strip()
        if c_clean and c_clean in clean_low:
            kategori = c
            break

    return jenis, kategori

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
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Rekap Keuangan"), KeyboardButton("📈 Grafik Visual")],
        [KeyboardButton("📋 Riwayat Transaksi"), KeyboardButton("🎯 Set Budget")],
        [KeyboardButton("💳 Dompet Saya"), KeyboardButton("🤝 Hutang & Piutang")],
        [KeyboardButton("📥 Tambah Pemasukan"), KeyboardButton("📤 Tambah Pengeluaran")],
        [KeyboardButton("📑 Export Laporan"), KeyboardButton("📦 Backup DB")],
        [KeyboardButton("❓ Bantuan"), KeyboardButton("❌ Batal")]
    ]
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

# Inisialisasi Database
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

        cursor.execute("PRAGMA table_info(transaksi)")
        columns = [column[1] for column in cursor.fetchall()]
        if "kategori" not in columns:
            cursor.execute("ALTER TABLE transaksi ADD COLUMN kategori TEXT DEFAULT 'Umum'")
        if "wallet" not in columns:
            cursor.execute("ALTER TABLE transaksi ADD COLUMN wallet TEXT DEFAULT '💵 Cash'")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_tgl ON transaksi(user_id, tanggal)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_jenis ON transaksi(user_id, jenis)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                budget_bulanan REAL DEFAULT 0,
                reminder_enabled INTEGER DEFAULT 1,
                wallet_initialized INTEGER DEFAULT 0
            )
        """)
        cursor.execute("PRAGMA table_info(settings)")
        s_cols = [c[1] for c in cursor.fetchall()]
        if "wallet_initialized" not in s_cols:
            cursor.execute("ALTER TABLE settings ADD COLUMN wallet_initialized INTEGER DEFAULT 0")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS custom_kategori (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                jenis TEXT,
                nama_kategori TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                nama_wallet TEXT,
                saldo REAL DEFAULT 0,
                UNIQUE(user_id, nama_wallet)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hutang_piutang (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                jenis TEXT,
                nama_pihak TEXT,
                nominal REAL,
                keterangan TEXT,
                tanggal TIMESTAMP,
                status TEXT DEFAULT 'Belum Lunas'
            )
        """)
        conn.commit()

# --- ENGINE MULTI-DOMPET & WALLET HELPER ---
DEFAULT_WALLETS = ["💵 Cash", "💳 Bank", "📱 E-Wallet"]

def init_user_wallets(user_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT wallet_initialized FROM settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        is_init = row[0] if row else 0

        if not is_init:
            for w in DEFAULT_WALLETS:
                cursor.execute(
                    "INSERT OR IGNORE INTO wallets (user_id, nama_wallet, saldo) VALUES (?, ?, 0)",
                    (user_id, w)
                )
            cursor.execute(
                "INSERT INTO settings (user_id, wallet_initialized) VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET wallet_initialized = 1",
                (user_id,)
            )
            conn.commit()

def get_user_wallets(user_id: int) -> dict:
    init_user_wallets(user_id)
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nama_wallet, saldo FROM wallets WHERE user_id = ?", (user_id,))
        return dict(cursor.fetchall())

def update_wallet_balance(user_id: int, wallet_name: str, change_amount: float):
    init_user_wallets(user_id)
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE wallets SET saldo = saldo + ? WHERE user_id = ? AND nama_wallet = ?",
            (change_amount, user_id, wallet_name)
        )
        conn.commit()

def detect_wallet_from_text(text: str, user_id: int = 0) -> str:
    wallets = get_user_wallets(user_id) if user_id else {}
    clean = re.sub(r'[^\w\s]', '', text.lower())
    
    if wallets:
        for w_name in wallets.keys():
            w_clean = strip_emoji(w_name).lower().strip()
            if w_clean and w_clean in clean:
                return w_name
            
    ewallet_kw = ["gopay", "ovo", "dana", "shopeepay", "shopee", "linkaja", "ewallet", "e-wallet", "qris"]
    bank_kw = ["bca", "mandiri", "bri", "bni", "cimb", "bank", "atm", "tf", "transfer", "rekening"]
    
    if any(k in clean for k in ewallet_kw):
        if wallets:
            for w_name in wallets.keys():
                if any(kw in w_name.lower() for kw in ["e-wallet", "gopay", "ovo", "dana"]):
                    return w_name
        return "📱 E-Wallet"
    elif any(k in clean for k in bank_kw):
        if wallets:
            for w_name in wallets.keys():
                if any(kw in w_name.lower() for kw in ["bank", "bca", "mandiri", "bri"]):
                    return w_name
        return "💳 Bank"
        
    if wallets:
        for w_name in wallets.keys():
            if "cash" in w_name.lower() or "tunai" in w_name.lower():
                return w_name
        return list(wallets.keys())[0]
    return "💵 Cash"

# --- HELPER PARSER HUTANG & PIUTANG ---
def detect_hutang_piutang(text: str):
    low = text.lower().strip()
    clean = re.sub(r'[^\w\s]', '', low)
    
    piutang_kw = ["pinjamkan", "kasih pinjam", "piutang", "utangin", "minjamin", "dipinjam", "dipinjamkan"]
    hutang_kw = ["pinjam dari", "hutang ke", "utang ke", "pinjem dari", "hutang", "utang", "ngutang", "berhutang", "pinjam", "pinjem"]

    has_piutang = any(k in clean for k in piutang_kw)
    has_hutang = any(k in clean for k in hutang_kw)
    
    if not (has_piutang or has_hutang):
        return None
        
    res = extract_nominal_from_sentence(text)
    if not res:
        return None
        
    nominal, ket, _ = res
    
    if has_piutang and not any(k in clean for k in ["hutang ke", "utang ke", "pinjam dari", "pinjem dari", "ngutang"]):
        jenis = "Piutang"
    else:
        jenis = "Hutang"

    nama = ket
    remove_words = [
        "pinjamkan", "kasih pinjam", "piutang", "utangin", "minjamin", "dipinjamkan", "dipinjam",
        "pinjam dari", "hutang ke", "utang ke", "pinjem dari", "hutang", "utang", "ngutang", "berhutang", "pinjam", "pinjem",
        "ke", "dari", "sama", "pada"
    ]
    for kw in remove_words:
        nama = re.sub(r'\b' + kw + r'\b', '', nama, flags=re.IGNORECASE).strip()
    
    nama = re.sub(r'\s+', ' ', nama).strip()
    if not nama:
        nama = "Tanpa Nama"
        
    return jenis, nominal, nama

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
        f"Halo <b>{user}</b>! Selamat datang di <b>Bot Catatan Keuangan Pribadi</b>. 💰📊\n\n"
        "💡 <b>Pencatatan Otomatis & Cerdas:</b>\n"
        "Anda bisa langsung mengetik pesan obrolan santai:\n"
        "• <code>beli ayam 15k gopay</code> ➡️ Potong saldo E-Wallet!\n"
        "• <code>pinjamkan 100k ke Budi</code> ➡️ Catat Piutang otomatis!\n\n"
        "<b>📌 Pilihan Menu Keyboard:</b>\n"
        "• 💳 <b>Dompet Saya</b> - Cek saldo Cash, Bank & E-Wallet\n"
        "• 🤝 <b>Hutang & Piutang</b> - Catat & pelunasan pinjaman\n"
        "• 📥 <b>Tambah Pemasukan</b> - Catat uang masuk per kategori\n"
        "• 📤 <b>Tambah Pengeluaran</b> - Catat pengeluaran per kategori\n"
        "• 📊 <b>Rekap Keuangan</b> - Cek total saldo & komparasi bulanan\n"
        "• 📈 <b>Grafik Visual</b> - Lihat grafik Pemasukan, Pengeluaran & Cashflow\n"
        "• 📋 <b>Riwayat Transaksi</b> - Edit, Hapus & Set tanggal manual\n"
        "• 🎯 <b>Set Budget</b> - Peringatan sisa anggaran bulanan\n"
        "• 📑 <b>Export Laporan</b> - Unduh laporan Excel (CSV) & PDF\n"
        "• 📦 <b>Backup DB</b> - Unduh file cadangan database"
    )
    await update.message.reply_text(
        pesan,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )

# Helper Penangani Tombol Menu Utama & Perintah Fleksibel
async def handle_if_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not await is_authorized(update):
        return True

    intent = match_flexible_command_intent(text)
    if not intent:
        return False

    if intent == "batal":
        await batal(update, context)
        return True
    elif intent == "rekap":
        await rekap(update, context)
        return True
    elif intent == "grafik":
        await prompt_grafik_menu(update, context)
        return True
    elif intent == "riwayat":
        await riwayat(update, context)
        return True
    elif intent == "prompt_pemasukan":
        await prompt_pemasukan(update, context)
        return True
    elif intent == "prompt_pengeluaran":
        await prompt_pengeluaran(update, context)
        return True
    elif intent == "budget":
        await prompt_set_budget(update, context)
        return True
    elif intent == "export":
        await prompt_export_menu(update, context)
        return True
    elif intent == "backup":
        await backup_db(update, context)
        return True
    elif intent == "dompet":
        await wallet_panel(update, context)
        return True
    elif intent == "hutang":
        await hutang_panel(update, context)
        return True
    elif intent == "bantuan":
        await start(update, context)
        return True

    return False

# Penangani Pesan Chat Teks Alami (NLP Auto-Detect)
async def handle_natural_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    text = update.message.text.strip()

    if await handle_if_menu_button(update, context, text):
        return

    user_id = update.effective_user.id
    waktu_sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Cek NLP Hutang / Piutang
    hp_res = detect_hutang_piutang(text)
    if hp_res:
        jenis_hp, nominal_hp, nama_pihak = hp_res
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO hutang_piutang (user_id, jenis, nama_pihak, nominal, keterangan, tanggal, status) VALUES (?, ?, ?, ?, ?, ?, 'Belum Lunas')",
                (user_id, jenis_hp, nama_pihak, nominal_hp, text, waktu_sekarang)
            )
            hp_id = cursor.lastrowid
            conn.commit()

        icon = "🟢" if jenis_hp == "Piutang" else "🔴"
        pesan_hp = (
            f"✅ <b>Catatan {jenis_hp} Berhasil Disimpan!</b> #{hp_id}\n\n"
            f"{icon} <b>Jenis:</b> {jenis_hp}\n"
            f"👤 <b>Nama Pihak:</b> {nama_pihak}\n"
            f"💰 <b>Nominal:</b> {format_rupiah(nominal_hp)}\n"
            f"📅 <b>Waktu:</b> {waktu_sekarang}\n\n"
            "💡 <i>Gunakan menu <b>🤝 Hutang & Piutang</b> untuk melihat atau menandai lunas.</i>"
        )
        await update.message.reply_text(pesan_hp, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
        return

    # Cek Transaksi Standar
    res = extract_nominal_from_sentence(text)
    if not res:
        return

    nominal, keterangan, original_text = res
    jenis, kategori = detect_jenis_and_kategori(original_text, user_id)
    wallet = detect_wallet_from_text(original_text, user_id)

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transaksi (user_id, jenis, nominal, keterangan, tanggal, kategori, wallet) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, jenis, nominal, keterangan, waktu_sekarang, kategori, wallet)
        )
        tx_id = cursor.lastrowid
        conn.commit()

    # Update saldo dompet
    change = nominal if jenis == "Pemasukan" else -nominal
    update_wallet_balance(user_id, wallet, change)

    emoji = "📥" if jenis == "Pemasukan" else "📤"
    pesan_sukses = (
        f"✅ <b>{jenis} Berhasil Dicatat Otomatis!</b> #{tx_id}\n\n"
        f"{emoji} <b>Nominal:</b> {format_rupiah(nominal)}\n"
        f"🏷️ <b>Kategori:</b> {kategori}\n"
        f"💳 <b>Dompet:</b> {wallet}\n"
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
            InlineKeyboardButton("📆 Pilih Tanggal/Bulan", callback_data="rekap_custom_prompt"),
            InlineKeyboardButton("♾️ Semua Waktu", callback_data="rekap_all"),
        ],
        [
            InlineKeyboardButton("📊 Komparasi Bulan Ini vs Lalu", callback_data="rekap_monthly_comp")
        ],
        [
            InlineKeyboardButton("📈 Menu Grafik Visual", callback_data="chart_menu_prompt")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# Generator Komparasi Bulanan
def get_monthly_comparison(user_id: int) -> str:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT SUM(nominal) FROM transaksi WHERE user_id = ? AND jenis = 'Pengeluaran' AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')",
            (user_id,)
        )
        keluar_ini = cursor.fetchone()[0] or 0.0

        cursor.execute(
            "SELECT SUM(nominal) FROM transaksi WHERE user_id = ? AND jenis = 'Pemasukan' AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime')",
            (user_id,)
        )
        masuk_ini = cursor.fetchone()[0] or 0.0

        cursor.execute(
            "SELECT SUM(nominal) FROM transaksi WHERE user_id = ? AND jenis = 'Pengeluaran' AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime', '-1 month')",
            (user_id,)
        )
        keluar_lalu = cursor.fetchone()[0] or 0.0

        cursor.execute(
            "SELECT SUM(nominal) FROM transaksi WHERE user_id = ? AND jenis = 'Pemasukan' AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now', 'localtime', '-1 month')",
            (user_id,)
        )
        masuk_lalu = cursor.fetchone()[0] or 0.0

    diff_keluar = keluar_ini - keluar_lalu
    str_pct_keluar = f"{(diff_keluar / keluar_lalu * 100):+.1f}%" if keluar_lalu > 0 else "N/A"

    diff_masuk = masuk_ini - masuk_lalu
    str_pct_masuk = f"{(diff_masuk / masuk_lalu * 100):+.1f}%" if masuk_lalu > 0 else "N/A"

    status_hemat = "🟢 <b>Lebih Hemat!</b>" if diff_keluar <= 0 else "🔴 <b>Lebih Boros!</b>"

    pesan = (
        "📊 <b>Analisis Komparasi Bulanan</b>\n"
        "<i>(Membandingkan Bulan Ini vs Bulan Lalu)</i>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "📤 <b>Pengeluaran:</b>\n"
        f"• Bulan Lalu: {format_rupiah(keluar_lalu)}\n"
        f"• Bulan Ini: {format_rupiah(keluar_ini)}\n"
        f"• Selisih: <b>{format_rupiah(abs(diff_keluar))}</b> ({str_pct_keluar}) -> {status_hemat}\n\n"
        "📥 <b>Pemasukan:</b>\n"
        f"• Bulan Lalu: {format_rupiah(masuk_lalu)}\n"
        f"• Bulan Ini: {format_rupiah(masuk_ini)}\n"
        f"• Selisih: <b>{format_rupiah(abs(diff_masuk))}</b> ({str_pct_masuk})\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Tips: Jaga pengeluaran bulan ini agar tetap terkontrol di bawah budget bulanan Anda!</i>"
    )
    return pesan

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
            if len(custom_ym) == 10:
                try:
                    dt_obj = datetime.strptime(custom_ym, "%Y-%m-%d")
                    judul = f"Tanggal {dt_obj.strftime('%d-%m-%Y')}"
                except ValueError:
                    judul = f"Tanggal {custom_ym}"
                where_clause = "user_id = ? AND strftime('%Y-%m-%d', tanggal) = ?"
            else:
                try:
                    parts = custom_ym.split("-")
                    judul = f"Periode {parts[1]}-{parts[0]}"
                except Exception:
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
            if len(custom_ym) == 10:
                try:
                    dt_obj = datetime.strptime(custom_ym, "%Y-%m-%d")
                    judul = f"Tanggal {dt_obj.strftime('%d-%m-%Y')}"
                except ValueError:
                    judul = f"Tanggal {custom_ym}"
                where_clause = "user_id = ? AND strftime('%Y-%m-%d', tanggal) = ?"
            else:
                try:
                    parts = custom_ym.split("-")
                    judul = f"Periode {parts[1]}-{parts[0]}"
                except Exception:
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
    wallets = get_user_wallets(user_id)
    total_wallet_saldo = sum(wallets.values())

    saldo_emoji = "💰" if saldo >= 0 else "⚠️"

    pesan_rekap = (
        f"📊 <b>Ringkasan Keuangan ({judul})</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 <b>Total Pemasukan Periode Ini:</b> {format_rupiah(total_masuk)}\n"
        f"📤 <b>Total Pengeluaran Periode Ini:</b> {format_rupiah(total_keluar)}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{saldo_emoji} <b>Cashflow Periode Ini:</b> <code>{format_rupiah(saldo)}</code>\n\n"
        "💳 <b>Rincian Saldo Dompet & Rekening:</b>\n"
    )

    for w_name, w_balance in wallets.items():
        pesan_rekap += f"• {w_name}: <b>{format_rupiah(w_balance)}</b>\n"

    pesan_rekap += f"👉 <b>Total Seluruh Saldo Real-time:</b> <code>{format_rupiah(total_wallet_saldo)}</code>\n\n"

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

# --- FITUR GRAFIK VISUAL LENGKAP ---
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

# Generator Bar Chart Cashflow
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
        target = "riwayat" if query.data == "rw_custom_prompt" else "rekap"
        context.user_data["custom_date_target"] = target
        await query.message.reply_text(
            "📆 <b>Set Filter Tanggal / Bulan Manual</b>\n\n"
            "Silakan kirimkan tanggal atau bulan yang ingin Anda lihat:\n"
            "• <b>Format Tanggal:</b> <code>DD-MM-YYYY</code> (Contoh: <code>15-08-2026</code> atau <code>01-07-2026</code>)\n"
            "• <b>Format Bulan:</b> <code>MM-YYYY</code> (Contoh: <code>08-2026</code> atau <code>07-2026</code>)\n\n"
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

    period_val = None

    # Cek format Tanggal Spesifik (DD-MM-YYYY)
    match_date = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", text)
    if match_date:
        tgl, bln, thn = int(match_date.group(1)), int(match_date.group(2)), match_date.group(3)
        if 1 <= bln <= 12 and 1 <= tgl <= 31:
            period_val = f"d_{thn}-{bln:02d}-{tgl:02d}"

    # Cek format Bulan/Tahun (MM-YYYY)
    if not period_val:
        match_ym = re.match(r"^(\d{1,2})[-/](\d{4})$", text)
        if match_ym:
            bln, thn = int(match_ym.group(1)), match_ym.group(2)
            if 1 <= bln <= 12:
                period_val = f"m_{thn}-{bln:02d}"

    if not period_val:
        await update.message.reply_text(
            "❌ Format salah!\n\n"
            "Harap kirimkan format:\n"
            "• <b>Tanggal:</b> <code>DD-MM-YYYY</code> (Contoh: <code>15-08-2026</code>)\n"
            "• <b>Bulan:</b> <code>MM-YYYY</code> (Contoh: <code>08-2026</code>)\n\n"
            "Silakan coba lagi:",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
        return CUSTOM_DATE_STATE

    target = context.user_data.get("custom_date_target", "rekap")
    if target == "riwayat":
        await riwayat(update, context, period=period_val, page=1)
    else:
        custom_ym = period_val[2:]
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
        cursor.execute("SELECT jenis, nominal, wallet FROM transaksi WHERE id = ? AND user_id = ?", (tx_id, user_id))
        row = cursor.fetchone()
        if row:
            jenis, old_nominal, wallet = row
            wallet = wallet if wallet else "💵 Cash"
            diff = nominal - old_nominal
            change = diff if jenis == "Pemasukan" else -diff
            update_wallet_balance(user_id, wallet, change)

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
    if not await is_authorized(update):
        return

    if not os.path.exists(DB_NAME):
        await update.message.reply_text("❌ File database belum ditemukan.")
        return

    await update.message.reply_document(
        document=open(DB_NAME, "rb"),
        filename="keuangan_backup.db",
        caption="📦 <b>Backup Database Keuangan</b>\n\nFile ini adalah cadangan database Anda. Simpan file ini dengan aman!",
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
        elif period == "all":
            judul_periode = "Semua Waktu"
            where_sql = "WHERE user_id = ?"
            params = (user_id,)
        elif period.startswith("d_"):
            date_str = period[2:]
            try:
                dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
                judul_periode = f"Tanggal {dt_obj.strftime('%d-%m-%Y')}"
            except ValueError:
                judul_periode = f"Tanggal {date_str}"
            where_sql = "WHERE user_id = ? AND strftime('%Y-%m-%d', tanggal) = ?"
            params = (user_id, date_str)
        elif period.startswith("m_"):
            ym_str = period[2:]
            try:
                parts = ym_str.split("-")
                judul_periode = f"Bulan {parts[1]}-{parts[0]}"
            except Exception:
                judul_periode = f"Bulan {ym_str}"
            where_sql = "WHERE user_id = ? AND strftime('%Y-%m', tanggal) = ?"
            params = (user_id, ym_str)
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

    filter_row_1 = [
        InlineKeyboardButton("📅 Hari Ini", callback_data="rw_today_1"),
        InlineKeyboardButton("🗓️ Bulan Ini", callback_data="rw_this_month_1"),
        InlineKeyboardButton("♾️ Semua", callback_data="rw_all_1"),
    ]
    filter_row_2 = [
        InlineKeyboardButton("📆 Set Tanggal Manual", callback_data="rw_custom_prompt")
    ]
    inline_keyboard.append(filter_row_1)
    inline_keyboard.append(filter_row_2)

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

# --- PANEL DOMPET SAYA & MANAJER HUTANG PIUTANG ---
async def wallet_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    wallets = get_user_wallets(user_id)
    total_saldo = sum(wallets.values())

    if not wallets:
        pesan = (
            "💳 <b>Dompet & Rekening Saya</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <i>Belum ada dompet atau rekening. Silakan tekan <b>➕ Tambah Dompet Baru</b> di bawah!</i>"
        )
    else:
        pesan = (
            "💳 <b>Dompet & Rekening Saya</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
        )
        for name, balance in wallets.items():
            pesan += f"• {name}: <b>{format_rupiah(balance)}</b>\n"
        pesan += (
            "━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Total Seluruh Saldo:</b> <code>{format_rupiah(total_saldo)}</code>\n\n"
            "💡 <i>Gunakan nama dompet pada obrolan (misal: <code>50k makan gopay</code> atau <code>100k gaji bca</code>) untuk otomatis memilih dompet!</i>"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Dompet Baru", callback_data="w_add_prompt")],
        [
            InlineKeyboardButton("✏️ Set Saldo Dompet", callback_data="w_set_select"),
            InlineKeyboardButton("🏷️ Edit Nama Dompet", callback_data="w_editname_select")
        ],
        [
            InlineKeyboardButton("🔄 Transfer Antar Dompet", callback_data="w_tf_select"),
            InlineKeyboardButton("🗑️ Hapus Dompet", callback_data="w_del_select")
        ]
    ])

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)

# Handler CRUD Dompet
async def prompt_add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.message.reply_text(
            "➕ <b>Tambah Dompet / Rekening Baru</b>\n\n"
            "Silakan kirimkan nama dompet/rekening baru yang ingin Anda buat.\n"
            "<i>Contoh:</i> <code>💳 Bank BCA</code>, <code>📱 GoPay</code>, <code>💳 Mandiri</code>, atau <code>💵 Kas Saku</code>\n\n"
            "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
    return ADD_WALLET_NAME_STATE

async def simpan_add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    user_id = update.effective_user.id
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO wallets (user_id, nama_wallet, saldo) VALUES (?, ?, 0)",
            (user_id, text)
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ Dompet / Rekening <b>{text}</b> berhasil ditambahkan!",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    await wallet_panel(update, context)
    return ConversationHandler.END

async def prompt_set_wallet_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        wallet_name = query.data.replace("w_set_name_", "")
        context.user_data["target_wallet_name"] = wallet_name

        await query.message.reply_text(
            f"✏️ <b>Set Saldo Manual ({wallet_name})</b>\n\n"
            "Silakan kirimkan nominal saldo baru untuk dompet ini.\n"
            "<i>Contoh:</i> <code>2.500.000</code>, <code>2.5jt</code>, atau <code>0</code>\n\n"
            "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
    return SET_WALLET_SALDO_STATE

async def simpan_set_wallet_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    try:
        nominal = parse_nominal(text)
    except ValueError:
        await update.message.reply_text("❌ Nominal angka tidak valid. Silakan coba lagi:")
        return SET_WALLET_SALDO_STATE

    wallet_name = context.user_data.get("target_wallet_name", "💵 Cash")
    user_id = update.effective_user.id

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE wallets SET saldo = ? WHERE user_id = ? AND nama_wallet = ?",
            (nominal, user_id, wallet_name)
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ Saldo <b>{wallet_name}</b> berhasil diatur ke <b>{format_rupiah(nominal)}</b>!",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    await wallet_panel(update, context)
    return ConversationHandler.END

async def prompt_transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        to_wallet = query.data.replace("w_tf_to_", "")
        context.user_data["tf_to_wallet"] = to_wallet
        from_wallet = context.user_data.get("tf_from_wallet", "")

        await query.message.reply_text(
            f"🔄 <b>Transfer Antar Dompet</b>\n\n"
            f"Darimana: <b>{from_wallet}</b>\n"
            f"Ke: <b>{to_wallet}</b>\n\n"
            "Silakan kirimkan nominal yang ingin ditransfer.\n"
            "<i>Contoh:</i> <code>100k</code>, <code>500.000</code>\n\n"
            "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
    return TRANSFER_WALLET_AMOUNT_STATE

async def simpan_transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    try:
        nominal = parse_nominal(text)
    except ValueError:
        await update.message.reply_text("❌ Nominal angka tidak valid. Silakan coba lagi:")
        return TRANSFER_WALLET_AMOUNT_STATE

    from_wallet = context.user_data.get("tf_from_wallet")
    to_wallet = context.user_data.get("tf_to_wallet")
    user_id = update.effective_user.id

    update_wallet_balance(user_id, from_wallet, -nominal)
    update_wallet_balance(user_id, to_wallet, nominal)

    await update.message.reply_text(
        f"✅ Berhasil mentransfer <b>{format_rupiah(nominal)}</b> dari <b>{from_wallet}</b> ke <b>{to_wallet}</b>!",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    await wallet_panel(update, context)
    return ConversationHandler.END

async def prompt_edit_wallet_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        wallet_name = query.data.replace("w_editname_item_", "")
        context.user_data["target_edit_wallet_name"] = wallet_name

        await query.message.reply_text(
            f"🏷️ <b>Ubah Nama Dompet ({wallet_name})</b>\n\n"
            "Silakan kirimkan nama dompet / rekening baru yang Anda inginkan.\n"
            "<i>Contoh:</i> <code>💳 BCA Syariah</code>, <code>📱 GoPay Utama</code>, <code>💳 Bank Mandiri</code>\n\n"
            "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
    return EDIT_WALLET_NAME_STATE

async def simpan_edit_wallet_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    old_wallet_name = context.user_data.get("target_edit_wallet_name")
    user_id = update.effective_user.id

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE wallets SET nama_wallet = ? WHERE user_id = ? AND nama_wallet = ?",
            (text, user_id, old_wallet_name)
        )
        cursor.execute(
            "UPDATE transaksi SET wallet = ? WHERE user_id = ? AND wallet = ?",
            (text, user_id, old_wallet_name)
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ Nama dompet <b>{old_wallet_name}</b> berhasil diubah menjadi <b>{text}</b>!",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    await wallet_panel(update, context)
    return ConversationHandler.END

async def hutang_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, jenis, nama_pihak, nominal, keterangan, tanggal FROM hutang_piutang WHERE user_id = ? AND status = 'Belum Lunas' ORDER BY id DESC",
            (user_id,)
        )
        rows = cursor.fetchall()

    buttons = [
        [
            InlineKeyboardButton("🔴 Tambah Hutang Saya", callback_data="hp_add_hutang"),
            InlineKeyboardButton("🟢 Tambah Piutang", callback_data="hp_add_piutang")
        ]
    ]

    if not rows:
        pesan = (
            "🤝 <b>Catatan Hutang & Piutang</b>\n\n"
            "🎉 <i>Tidak ada tanggungan hutang atau piutang aktif saat ini! Semua lunas.</i>\n\n"
            "💡 <i>Gunakan tombol di atas untuk mencatat manual, atau ketik obrolan seperti: <code>utang 100k ke Budi</code> / <code>pinjamkan 50k ke Andi</code>!</i>"
        )
    else:
        pesan = "🤝 <b>Daftar Hutang & Piutang Aktif</b>\n\n"
        tot_piutang = 0
        tot_hutang = 0

        for r in rows:
            hp_id, jenis, nama, nom, ket, tgl = r
            if jenis == "Piutang":
                tot_piutang += nom
                icon = "🟢"
                desc = f"Berhutang ke Anda: <b>{nama}</b>"
            else:
                tot_hutang += nom
                icon = "🔴"
                desc = f"Anda berhutang ke: <b>{nama}</b>"

            pesan += f"{icon} <b>#{hp_id} [{jenis}]</b> {format_rupiah(nom)}\n"
            pesan += f"   👤 {desc}\n   📅 {tgl}\n\n"
            buttons.append([InlineKeyboardButton(f"✅ Tandai Lunas #{hp_id}", callback_data=f"lunas_hp_{hp_id}")])

        pesan += (
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 Total Piutang (Uang di orang lain): <b>{format_rupiah(tot_piutang)}</b>\n"
            f"🔴 Total Hutang (Tanggungan Anda): <b>{format_rupiah(tot_hutang)}</b>\n"
        )

    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(pesan, parse_mode=ParseMode.HTML, reply_markup=keyboard)

# Handler Form Input Hutang / Piutang
async def prompt_add_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        jenis = "Hutang" if query.data == "hp_add_hutang" else "Piutang"
        context.user_data["hp_jenis"] = jenis
        
        if jenis == "Hutang":
            title = "🔴 <b>Catatan Hutang Baru (Tanggungan Anda)</b>"
            desc = "Silakan kirimkan <b>nama orang / pihak</b> tempat Anda berhutang.\n<i>Contoh:</i> <code>Budi</code>, <code>Bank Mandiri</code>, <code>Ahmad</code>"
        else:
            title = "🟢 <b>Catatan Piutang Baru (Uang Anda di Orang Lain)</b>"
            desc = "Silakan kirimkan <b>nama orang / pihak</b> yang meminjam uang dari Anda.\n<i>Contoh:</i> <code>Budi</code>, <code>Siti</code>, <code>Doni</code>"

        await query.message.reply_text(
            f"{title}\n\n{desc}\n\n💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
    return INPUT_HP_NAME_STATE

async def simpan_hp_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    context.user_data["hp_nama_pihak"] = text
    jenis = context.user_data.get("hp_jenis", "Hutang")

    await update.message.reply_text(
        f"💰 <b>Nominal {jenis} ({text})</b>\n\n"
        "Silakan kirimkan nominal angkanya.\n"
        "<i>Contoh:</i> <code>100k</code>, <code>1.500.000</code>, <code>50rb</code>\n\n"
        "💡 <i>Ketik atau tekan <b>❌ Batal</b> untuk membatalkan.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    return INPUT_HP_NOMINAL_STATE

async def simpan_hp_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if await handle_if_menu_button(update, context, text):
        return ConversationHandler.END

    try:
        nominal = parse_nominal(text)
    except ValueError:
        await update.message.reply_text("❌ Nominal angka tidak valid. Silakan kirimkan lagi (misal: <code>100k</code>):")
        return INPUT_HP_NOMINAL_STATE

    jenis = context.user_data.get("hp_jenis", "Hutang")
    nama_pihak = context.user_data.get("hp_nama_pihak", "Tanpa Nama")
    user_id = update.effective_user.id
    waktu_sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO hutang_piutang (user_id, jenis, nama_pihak, nominal, keterangan, tanggal, status) VALUES (?, ?, ?, ?, ?, ?, 'Belum Lunas')",
            (user_id, jenis, nama_pihak, nominal, f"{jenis} ke {nama_pihak}", waktu_sekarang)
        )
        hp_id = cursor.lastrowid
        conn.commit()

    icon = "🔴" if jenis == "Hutang" else "🟢"
    await update.message.reply_text(
        f"✅ <b>Catatan {jenis} Berhasil Disimpan!</b> #{hp_id}\n\n"
        f"{icon} <b>Jenis:</b> {jenis}\n"
        f"👤 <b>Nama Pihak:</b> {nama_pihak}\n"
        f"💰 <b>Nominal:</b> {format_rupiah(nominal)}\n"
        f"📅 <b>Waktu:</b> {waktu_sekarang}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    await hutang_panel(update, context)
    return ConversationHandler.END

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

    elif data == "do_export_csv":
        await export_csv(update, context)
    elif data == "do_export_pdf":
        await export_pdf(update, context)

    elif data == "chart_menu_prompt":
        await prompt_grafik_menu(update, context)
    elif data == "do_chart_pengeluaran":
        await kirim_grafik_pie(update, context, jenis="Pengeluaran")
    elif data == "do_chart_pemasukan":
        await kirim_grafik_pie(update, context, jenis="Pemasukan")
    elif data == "do_chart_cashflow":
        await kirim_grafik_cashflow(update, context)

    elif data == "rekap_today":
        await query.answer()
        await rekap(update, context, period="today")
    elif data == "rekap_this_month":
        await query.answer()
        await rekap(update, context, period="this_month")
    elif data == "rekap_all":
        await query.answer()
        await rekap(update, context, period="all")
    elif data == "rekap_monthly_comp":
        await query.answer()
        comp_msg = get_monthly_comparison(user_id)
        await query.message.reply_text(comp_msg, parse_mode=ParseMode.HTML, reply_markup=get_rekap_keyboard())

    elif data == "w_set_select":
        await query.answer()
        wallets = get_user_wallets(user_id)
        buttons = []
        for name in wallets.keys():
            buttons.append([InlineKeyboardButton(f"✏️ Set Saldo {name}", callback_data=f"w_set_name_{name}")])
        buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="w_back")])
        await query.edit_message_text("✏️ Pilih dompet/rekening yang ingin diatur saldonya:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "w_editname_select":
        await query.answer()
        wallets = get_user_wallets(user_id)
        buttons = []
        for name in wallets.keys():
            buttons.append([InlineKeyboardButton(f"🏷️ Edit Nama {name}", callback_data=f"w_editname_item_{name}")])
        buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="w_back")])
        await query.edit_message_text("🏷️ Pilih dompet/rekening yang ingin diubah namanya:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "w_tf_select":
        await query.answer()
        wallets = get_user_wallets(user_id)
        buttons = []
        for name in wallets.keys():
            buttons.append([InlineKeyboardButton(f"Darimana: {name}", callback_data=f"w_tf_from_{name}")])
        buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="w_back")])
        await query.edit_message_text("🔄 Pilih <b>Dompet Asal</b> (sumber dana):", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("w_tf_from_"):
        await query.answer()
        from_wallet = data.replace("w_tf_from_", "")
        context.user_data["tf_from_wallet"] = from_wallet
        wallets = get_user_wallets(user_id)
        buttons = []
        for name in wallets.keys():
            if name != from_wallet:
                buttons.append([InlineKeyboardButton(f"Ke: {name}", callback_data=f"w_tf_to_{name}")])
        buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="w_back")])
        await query.edit_message_text(f"🔄 Transfer dari <b>{from_wallet}</b>.\nPilih <b>Dompet Tujuan</b>:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "w_del_select":
        await query.answer()
        wallets = get_user_wallets(user_id)
        buttons = []
        for name in wallets.keys():
            buttons.append([InlineKeyboardButton(f"🗑️ Hapus {name}", callback_data=f"w_del_name_{name}")])
        buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="w_back")])
        await query.edit_message_text("🗑️ Pilih dompet/rekening yang ingin dihapus:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("w_del_name_"):
        wallet_name = data.replace("w_del_name_", "")
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM wallets WHERE user_id = ? AND nama_wallet = ?", (user_id, wallet_name))
            conn.commit()
        await query.answer(f"✅ Dompet {wallet_name} berhasil dihapus!", show_alert=True)
        await wallet_panel(update, context)

    elif data == "w_back":
        await query.answer()
        await wallet_panel(update, context)

    elif data.startswith("lunas_hp_"):
        hp_id = int(data.split("_")[2])
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE hutang_piutang SET status = 'Lunas' WHERE id = ? AND user_id = ?", (hp_id, user_id))
            conn.commit()
        await query.answer("✅ Catatan berhasil ditandai Lunas!", show_alert=True)
        await hutang_panel(update, context)

    elif data.startswith("rw_"):
        await query.answer()
        raw = data[3:]
        parts = raw.split("_")
        if len(parts) > 1 and parts[-1].isdigit():
            page = int(parts[-1])
            period = "_".join(parts[:-1])
        else:
            page = 1
            period = raw
        await riwayat(update, context, period=period, page=page)

    elif data.startswith("editopt_"):
        await query.answer()
        raw = data[8:]
        parts = raw.split("_")
        tx_id = int(parts[0])
        page = int(parts[-1]) if parts[-1].isdigit() else 1
        period = "_".join(parts[1:-1]) if len(parts) > 2 else parts[1]

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

    elif data.startswith("askdel_"):
        await query.answer()
        raw = data[7:]
        parts = raw.split("_")
        tx_id = int(parts[0])
        page = int(parts[-1]) if parts[-1].isdigit() else 1
        period = "_".join(parts[1:-1]) if len(parts) > 2 else parts[1]

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

    elif data.startswith("confirmdel_"):
        raw = data[11:]
        parts = raw.split("_")
        tx_id = int(parts[0])
        page = int(parts[-1]) if parts[-1].isdigit() else 1
        period = "_".join(parts[1:-1]) if len(parts) > 2 else parts[1]

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT jenis, nominal, wallet FROM transaksi WHERE id = ? AND user_id = ?", (tx_id, user_id))
            row = cursor.fetchone()
            if row:
                jenis, nominal, wallet = row
                wallet = wallet if wallet else "💵 Cash"
                change = -nominal if jenis == "Pemasukan" else nominal
                update_wallet_balance(user_id, wallet, change)

            cursor.execute("DELETE FROM transaksi WHERE id = ? AND user_id = ?", (tx_id, user_id))
            conn.commit()

        await query.answer(f"✅ Transaksi #{tx_id} berhasil dihapus!", show_alert=True)
        await riwayat(update, context, period=period, page=page)


# --- DAILY REMINDER JOB ---
async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT user_id FROM transaksi")
        user_ids = [row[0] for row in cursor.fetchall()]

    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "🔔 <b>Pengingat Catat Keuangan</b>\n\n"
                    "Halo! Sudahkah Anda mencatat pengeluaran & pemasukan Anda hari ini? 😉\n\n"
                    "Cukup ketik obrolan seperti <code>beli ayam 15k</code> atau <code>dapat uang 500k</code>!"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logging.error(f"Gagal mengirim reminder ke {uid}: {e}")

if __name__ == "__main__":
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📥 Tambah Pemasukan$"), prompt_pemasukan),
            MessageHandler(filters.Regex("^📤 Tambah Pengeluaran$"), prompt_pengeluaran),
            MessageHandler(filters.Regex("^🎯 Set Budget$"), prompt_set_budget),
            CommandHandler("masuk", prompt_pemasukan),
            CommandHandler("keluar", prompt_pengeluaran),
            CommandHandler("setbudget", prompt_set_budget),
            CallbackQueryHandler(prompt_custom_date, pattern="^rekap_custom_prompt$"),
            CallbackQueryHandler(prompt_custom_date, pattern="^rw_custom_prompt$"),
            CallbackQueryHandler(prompt_edit_nominal, pattern="^doedit_nom_"),
            CallbackQueryHandler(prompt_edit_keterangan, pattern="^doedit_ket_"),
            CallbackQueryHandler(prompt_add_wallet, pattern="^w_add_prompt$"),
            CallbackQueryHandler(prompt_set_wallet_saldo, pattern="^w_set_name_"),
            CallbackQueryHandler(prompt_edit_wallet_name, pattern="^w_editname_item_"),
            CallbackQueryHandler(prompt_transfer_amount, pattern="^w_tf_to_"),
            CallbackQueryHandler(prompt_add_hp, pattern="^hp_add_"),
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
            ADD_WALLET_NAME_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_add_wallet)],
            SET_WALLET_SALDO_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_set_wallet_saldo)],
            TRANSFER_WALLET_AMOUNT_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_transfer_amount)],
            EDIT_WALLET_NAME_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_edit_wallet_name)],
            INPUT_HP_NAME_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_hp_name)],
            INPUT_HP_NOMINAL_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, simpan_hp_nominal)],
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
    app.add_handler(CommandHandler("dompet", wallet_panel))
    app.add_handler(CommandHandler("hutang", hutang_panel))
    
    app.add_handler(MessageHandler(filters.Regex("^📊 Rekap Keuangan$"), rekap))
    app.add_handler(MessageHandler(filters.Regex("^📈 Grafik Visual$"), prompt_grafik_menu))
    app.add_handler(MessageHandler(filters.Regex("^📋 Riwayat Transaksi$"), riwayat))
    app.add_handler(MessageHandler(filters.Regex("^🎯 Set Budget$"), prompt_set_budget))
    app.add_handler(MessageHandler(filters.Regex("^💳 Dompet Saya$"), wallet_panel))
    app.add_handler(MessageHandler(filters.Regex("^🤝 Hutang & Piutang$"), hutang_panel))
    app.add_handler(MessageHandler(filters.Regex("^📑 Export Laporan$"), prompt_export_menu))
    app.add_handler(MessageHandler(filters.Regex("^📊 Export CSV$"), prompt_export_menu))
    app.add_handler(MessageHandler(filters.Regex("^📦 Backup DB$"), backup_db))
    app.add_handler(MessageHandler(filters.Regex("^❓ Bantuan$"), start))
    app.add_handler(MessageHandler(filters.Regex("^❌ Batal$"), batal))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_natural_text_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    if app.job_queue:
        app.job_queue.run_daily(send_daily_reminder, time=time(hour=20, minute=0, second=0))

    print("🚀 Bot Keuangan Pribadi sedang berjalan...")
    app.run_polling()
