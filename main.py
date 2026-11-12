import os
from pathlib import Path
from dotenv import load_dotenv
from pypdf import PdfReader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Memuat environment variable dari file .env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:

    raise ValueError("TELEGRAM_BOT_TOKEN belum dikonfigurasi di file .env")

# Harga per halaman (dapat disesuaikan dengan kebutuhan)
HARGA_BW = 500      # Rp 500 / halaman (Hitam Putih)
HARGA_COLOR = 1500  # Rp 1.500 / halaman (Berwarna)


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
    file_name = doc.file_name or "dokumen.pdf"
    file_mime = doc.mime_type

    # 1. Cek Apakah File Berformat PDF
    if not file_name.lower().endswith(".pdf") and file_mime != "application/pdf":
        await update.message.reply_text("Berkas ditolak! Mohon kirimkan dokumen berformat PDF.")
        return

    await update.message.reply_text(f"File PDF diterima: {file_name}.\nSedang menghitung jumlah halaman...")

    # 2. Download Sementara File PDF untuk Dibaca Halamannya
    telegram_file = await context.bot.get_file(doc.file_id)
    local_filename = f"temp_{doc.file_id}.pdf"
    await telegram_file.download_to_drive(local_filename)

    try:
        # 3. Hitung Jumlah Halaman PDF Menggunakan PyPDF
        reader = PdfReader(local_filename)
        total_pages = len(reader.pages)

        # Simpan data sementara di memori sesi user (context.user_data)
        context.user_data["file_name"] = file_name
        context.user_data["total_pages"] = total_pages

        # 4. Buat Tombol Inline Keyboard untuk Opsi Warna
        keyboard = [
            [
                InlineKeyboardButton(
                    f"⬛ Hitam Putih (Rp {HARGA_BW:,}/hal)",
                    callback_data="color_bw",
                ),
                InlineKeyboardButton(
                    f"🌈 Berwarna (Rp {HARGA_COLOR:,}/hal)",
                    callback_data="color_full",
                ),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # 5. Balas ke Pengguna dengan Rincian & Tombol Pilihan
        await update.message.reply_text(
            f"✅ Dokumen Valid!\n\n"
            f"📄 Nama Berkas: {file_name}\n"
            f"📊 Total Halaman: {total_pages} halaman\n\n"
            f"Silakan pilih opsi warna cetakan di bawah ini:",
            reply_markup=reply_markup,
        )

    except Exception:
        await update.message.reply_text("Gagal membaca file PDF. Pastikan file tidak rusak atau terkunci.")

    finally:
        # 6. Hapus File Temporary Agar Hemat Storage
        if os.path.exists(local_filename):
            os.remove(local_filename)


# Handler saat pengguna menekan tombol opsi warna
async def handle_color_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # Menghilangkan ikon loading pada tombol Telegram

    pilihan = query.data
    file_name = context.user_data.get("file_name", "Dokumen PDF")
    total_pages = context.user_data.get("total_pages", 0)

    if total_pages == 0:
        await query.edit_message_text("Sesi dokumen tidak ditemukan. Silakan kirim ulang file PDF kamu.")
        return

    if pilihan == "color_bw":
        mode_cetak = "Hitam Putih (Grayscale)"
        harga_satuan = HARGA_BW
    else:
        mode_cetak = "Berwarna (Full Color)"
        harga_satuan = HARGA_COLOR

    total_biaya = total_pages * harga_satuan

    # Edit pesan tombol menjadi ringkasan pesanan cetak
    await query.edit_message_text(
        f"🖨️ **Konfigurasi Cetak Dikonfirmasi**\n\n"
        f"📄 Nama Berkas: {file_name}\n"
        f"📊 Total Halaman: {total_pages} halaman\n"
        f"🎨 Opsi Warna: {mode_cetak}\n"
        f"💵 Estimasi Biaya: Rp {total_biaya:,}\n\n"
        f"(Siap dilanjutkan ke tahap jumlah rangkap / pembayaran QRIS).",
        parse_mode="Markdown",
    )


# Fungsi Utama untuk Menjalankan Bot
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    # Daftarkan Handler Perintah, Pesan, dan Tombol Callback
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_color_choice, pattern="^color_"))

    print("Bot VendingPrint sedang berjalan... Tekan Ctrl+C untuk berhenti.")
    app.run_polling()