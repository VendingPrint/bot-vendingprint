import os
from pathlib import Path
from dotenv import load_dotenv
from pypdf import PdfReader
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Memuat environment variable dari file .env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Ganti dengan Token ARI @Botfather kamu
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN belum dikonfigurasi di file .env")

# Handler ketika user ketik /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"Halo {user_name}! Selamat datang di VendingPrint Bot.\n"
        f"Silakan kirim dokumen berformat PDF yang ingin kamu cetak."
    )

# Handler untuk membalas pesan teks biasa (Tanya-Jawab dasar)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_masuk = update.message.text.lower()
    
    if "halo" in text_masuk or "hi" in text_masuk:
        await update.message.reply_text("Halo juga! Ada dokumen PDF yang mau dicetak hari ini?")
    elif "cara" in text_masuk or "gimana" in text_masuk:
        await update.message.reply_text("Caranya mudah: cukup kirimkan file dokumen PDF kamu ke chat ini ya.")
    else:
        await update.message.reply_text(
            f"Aku menerima pesanmu: '{update.message.text}'. "
            f"Kirimkan file bertipe PDF untuk mulai memproses dokumen."
        )

# Handler untuk menerima File / Dokumen
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = doc.file_name
    file_mime = doc.mime_type

    # 1. Cek Apakah File Berformat PDF
    if not file_name.endswith('.pdf') and file_mime != 'application/pdf':
        await update.message.reply_text("Berkas ditolak! Mohon kirimkan dokumen berformat PDF.")
        return

    await update.message.reply_text(f"File PDF diterima: {file_name}.\nSedang menghitung jumlah halaman...")

    # 2. Download Sementara File PDF ke Laptop/Server untuk Dibaca Halamannya
    telegram_file = await context.bot.get_file(doc.file_id)
    local_filename = f"temp_{doc.file_id}.pdf"
    await telegram_file.download_to_drive(local_filename)

    try:
        # 3. Hitung Jumlah Halaman PDF Menggunakan PyPDF
        reader = PdfReader(local_filename)
        total_pages = len(reader.pages)

        # 4. Balas ke Pengguna Jumlah Halaman PDF
        await update.message.reply_text(
            f"Dokumen Valid!\n\n"
            f"Nama Berkas: {file_name}\n"
            f"Total Halaman: {total_pages} halaman\n\n"
            f"(Modul awal berhasil! Siap disambungkan ke fitur konfigurasi cetak dan pembayaran)."
        )

    except Exception as e:
        await update.message.reply_text("Gagal membaca file PDF. Pastikan file tidak rusak.")
    
    finally:
        # 5. Hapus File Temporary Agar Hemat Storage
        if os.path.exists(local_filename):
            os.remove(local_filename)

# Fungsi Utama untuk Menjalankan Bot
if __name__ == '__main__':
    # Membangun Aplikasi Telegram Bot
    app = ApplicationBuilder().token(TOKEN).build()

    # Daftarkan Handler Perintah dan Pesan
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("Bot VendingPrint sedang berjalan... Tekan Ctrl+C untuk berhenti.")
    app.run_polling()
