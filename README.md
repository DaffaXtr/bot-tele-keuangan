# 💰 Bot Catatan Keuangan Pribadi (Telegram Bot)

Bot Telegram pintar, interaktif, dan fleksibel yang dirancang untuk membantu pengelolaan keuangan pribadi harian secara serba otomatis, cepat, dan nyaman. Bot ini mendukung banyak pengguna dengan data yang dipisahkan berdasarkan akun Telegram masing-masing.

![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)
![Telegram Bot API](https://img.shields.io/badge/Telegram%20Bot%20API-v20%2B-blue)
![Database](https://img.shields.io/badge/database-SQLite3-green)
![License](https://img.shields.io/badge/license-MIT-orange)

---

## ✨ Fitur-Fitur Utama

- **🧠 Smart Nominal & Keterangan Parser Bahasa Indonesia**
  Dapat membaca input gaya bahasa alami secara otomatis tanpa ribet:
  - `1,5 juta Sangu` ➡️ Rp 1.500.000 (Keterangan: Sangu)
  - `1.5jt Gaji` ➡️ Rp 1.500.000 (Keterangan: Gaji)
  - `15k Makan Siang` ➡️ Rp 15.000 (Keterangan: Makan Siang)
  - `15.000 Kopi` ➡️ Rp 15.000 (Keterangan: Kopi)

- **🔘 Menu Keyboard Interaktif**
  Tersedia tombol menu navigasi di bawah layar chat Telegram sehingga tidak perlu mengetik perintah manual.

- **📊 Rekap & Breakdown Kategori**
  Ringkasan total pemasukan, pengeluaran, serta sisa saldo dengan opsi filter waktu:
  - 📅 Hari Ini
  - 🗓️ Bulan Ini
  - 📆 Filter Custom Bulan/Tahun (`MM-YYYY`) atau Tanggal Spesifik (`DD-MM-YYYY`)
  - ♾️ Semua Waktu

- **📈 Visualisasi Grafik Visual (Matplotlib)**
  - **📤 Pie Chart Pengeluaran:** Persentase alokasi pengeluaran per kategori.
  - **📥 Pie Chart Pemasukan:** Persentase alokasi sumber uang masuk.
  - **📊 Bar Chart Cashflow:** Grafik batang perbandingan Total Pemasukan vs Total Pengeluaran.

- **📋 Riwayat Transaksi & Filter Tanggal Manual**
  Menampilkan riwayat transaksi per halaman (5 item/halaman) dilengkapi tombol **✏️ Edit Nominal/Kategori/Keterangan**, **🗑️ Hapus**, serta tombol **📆 Set Tanggal Manual** (`DD-MM-YYYY` / `MM-YYYY`).

- **🎯 Pengaturan Budget Bulanan**
  Fitur peringatan otomatis saat pengeluaran telah mencapai **80%** atau **melebihi (100%)** dari target budget bulanan yang diset.

- **📑 Export Laporan PDF & CSV (Excel)**
  - **📄 PDF Report:** Dokumen rapi berisi ringkasan saldo dan tabel rincian transaksi lengkap dengan kolom Nomor Urut (`No.`).
  - **📊 CSV File:** Format file laporan rapi yang siap dibuka di Microsoft Excel atau aplikasi spreadsheet lainnya.

- **📦 Backup Database Instan**
  Dapat mengunduh cadangan database `keuangan.db` sewaktu-waktu.

- **🔔 Pengingat Harian (Daily Reminder)**
  Pengingat otomatis harian setiap jam 20:00 WIB untuk mencatat pengeluaran.

---

## 🛠️ Teknologi yang Digunakan

- **Bahasa Pemrograman:** Python 3.10+
- **Bot Framework:** `python-telegram-bot` (v20+)
- **Database:** SQLite3 (Dilengkapi Indexing Kecepatan Tinggi `idx_tx_user_tgl` & `idx_tx_user_jenis`)
- **PDF Generator:** `fpdf2`
- **Graphic Generator:** `matplotlib` (Non-GUI Agg Backend)
- **Environment Management:** `python-dotenv`

---

## 🚀 Cara Menginstal & Menjalankan

### 1. Prasyarat
Pastikan Python 3.10+ sudah terinstal di komputer Anda.

### 2. Install Dependensi
Buka terminal / Command Prompt pada folder proyek ini dan jalankan:
```bash
pip install -r requirements.txt
```

### 3. Mengatur Konfigurasi (.env)
Salin berkas `.env.example` menjadi `.env`:
```bash
# Windows (PowerShell)
Copy-Item .env.example .env

# Linux / macOS
cp .env.example .env
```
Buka `.env` dan isi token Telegram Bot dari `@BotFather`:
```env
TELEGRAM_BOT_TOKEN="TOKEN_BOT_ANDA"
ALLOWED_USER_ID="username_atau_id_anda"
```

### 4. Menjalankan Bot
Jalankan bot dengan perintah:
```bash
python bot.py
```

---

## 💡 Panduan Singkat Format Input

| Input Pengguna | Nominal Dibaca | Keterangan |
| :--- | :--- | :--- |
| `1,5 juta Sangu` | Rp 1.500.000 | Sangu |
| `1.5jt Transfer` | Rp 1.500.000 | Transfer |
| `15ribu Makan` | Rp 15.000 | Makan |
| `20k Kopi` | Rp 20.000 | Kopi |
| `50.000 Bensin` | Rp 50.000 | Bensin |

---

## 📁 Struktur Berkas

```text
Bot-Keuangan/
├── .env.example     # Template konfigurasi environment variable
├── bot.py           # Script utama bot Telegram
├── keuangan.db      # Database SQLite (dibuat otomatis saat bot dijalankan)
├── README.md        # Dokumentasi proyek
└── requirements.txt # Daftar dependensi Python
```

---

## 📄 Lisensi
Proyek ini dibuat untuk pemakaian pribadi dan bebas dikembangkan lebih lanjut di bawah lisensi MIT.
