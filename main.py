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
    ConversationHandler,
    filters,
    ContextTypes,
)

# Memuat environment variable dari file .env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN belum dikonfigurasi di file .env")

# Harga per halaman
HARGA_BW = 500      # Rp 500 / halaman
HARGA_COLOR = 1500  # Rp 1.500 / halaman

# Tahapan (States) alur percakapan sesuai wireframe UI/UX
STEP_COLOR, STEP_COPIES, STEP_ORIENTATION, STEP_PAPER, STEP_PAGES = range(5)


# Fungsi validasi rentang halaman ("All", "1", "2", atau "1-3")
def parse_page_selection(text: str, max_pages: int):
    clean = text.strip().lower()
    if clean == "all":
        return list(range(1, max_pages + 1))

    pages = set()
    parts = clean.replace(" ", "").split(",")
    for part in parts:
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2 or not bounds[0].isdigit() or not bounds[1].isdigit():
                return None
            start, end = int(bounds[0]), int(bounds[1])
            if start < 1 or end > max_pages or start > end:
                return None
            pages.update(range(start, end + 1))
        else:
            if not part.isdigit():
                return None
            p = int(part)
            if p < 1 or p > max_pages:
                return None
            pages.add(p)
    return sorted(pages) if pages else None


# Command /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"Halo {user_name}! Selamat datang di **Bot VendingPrint**.\n"
        f"Silakan kirim **Dokumen PDF** yang ingin dicetak.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# 1. Menerima Dokumen PDF -> Lanjut ke Step 1 (Atur warna dokumen)
async def receive_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = doc.file_name or "dokumen.pdf"
    file_mime = doc.mime_type

    if not file_name.lower().endswith(".pdf") and file_mime != "application/pdf":
        await update.message.reply_text("Berkas ditolak! Mohon kirimkan dokumen berformat PDF.")
        return ConversationHandler.END

    telegram_file = await context.bot.get_file(doc.file_id)
    local_filename = f"temp_{doc.file_id}.pdf"
    await telegram_file.download_to_drive(local_filename)

    try:
        reader = PdfReader(local_filename)
        total_pages = len(reader.pages)

        context.user_data.clear()
        context.user_data["file_id"] = doc.file_id
        context.user_data["file_name"] = file_name
        context.user_data["total_doc_pages"] = total_pages

        keyboard = [
            [
                InlineKeyboardButton("B/W", callback_data="color_bw"),
                InlineKeyboardButton("Color", callback_data="color_full"),
            ]
        ]
        await update.message.reply_text(
            f"📄 Dokumen diterima: `{file_name}` ({total_pages} halaman)\n\n"
            f"Atur warna dokumen",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return STEP_COLOR

    except Exception:
        await update.message.reply_text("Gagal membaca file PDF. Pastikan file tidak rusak atau terkunci.")
        return ConversationHandler.END

    finally:
        if os.path.exists(local_filename):
            os.remove(local_filename)


# 2. Menangkap pilihan Warna -> Lanjut tanya jumlah rangkap/halaman
async def select_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "color_bw":
        context.user_data["color_mode"] = "B/W"
        pilihan_teks = "B/W"
    else:
        context.user_data["color_mode"] = "Color"
        pilihan_teks = "Color"

    await query.edit_message_text(f"Atur warna dokumen: **{pilihan_teks}** ✅", parse_mode="Markdown")
    await query.message.reply_text("Berapa halaman yang ingin dicetak? (Masukkan jumlah rangkap/kopi, contoh: 1)")
    return STEP_COPIES


# 3. Menangkap input angka jumlah rangkap -> Lanjut tanya Potrait / Landscape
async def input_copies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text.strip()
    if not teks.isdigit() or int(teks) < 1:
        await update.message.reply_text("Mohon masukkan angka yang valid (minimal 1), contoh: `1`", parse_mode="Markdown")
        return STEP_COPIES

    context.user_data["copies"] = int(teks)

    keyboard = [
        [
            InlineKeyboardButton("Potrait", callback_data="orient_Potrait"),
            InlineKeyboardButton("Landscape", callback_data="orient_Landscape"),
        ]
    ]
    await update.message.reply_text(
        "Potrait atau landscape?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return STEP_ORIENTATION


# 4. Menangkap pilihan Orientasi -> Lanjut tanya Ukuran Kertas (hanya A4 aktif)
async def select_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    orientasi = query.data.replace("orient_", "")
    context.user_data["orientation"] = orientasi

    await query.edit_message_text(f"Potrait atau landscape?: **{orientasi}** ✅", parse_mode="Markdown")

    keyboard = [
        [
            InlineKeyboardButton("F4 (Nonaktif)", callback_data="paper_disabled"),
            InlineKeyboardButton("A5 (Nonaktif)", callback_data="paper_disabled"),
            InlineKeyboardButton("A4", callback_data="paper_A4"),
        ]
    ]
    await query.message.reply_text(
        "Ukuran kertas?\n(untuk sekarang hanya bisa A4)",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return STEP_PAPER


# 5. Menangkap pilihan Ukuran Kertas -> Lanjut tanya Halaman mana yang mau diprint
async def select_paper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.data == "paper_disabled":
        await query.answer("Maaf, untuk sekarang ukuran kertas hanya tersedia A4.", show_alert=True)
        return STEP_PAPER

    await query.answer()
    context.user_data["paper_size"] = "A4"

    await query.edit_message_text("Ukuran kertas: **A4** ✅", parse_mode="Markdown")
    await query.message.reply_text(
        "Halaman mana yang mau diprint?\n"
        "(Contoh: All, 1, 2, atau 1-3)"
    )
    return STEP_PAGES


# 6. Menangkap pilihan Halaman -> Tampilkan Ringkasan Akhir & Siap Integrasi
async def input_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text.strip()
    max_pages = context.user_data.get("total_doc_pages", 1)

    selected_pages = parse_page_selection(teks, max_pages)
    if not selected_pages:
        await update.message.reply_text(
            f"⚠️ Input halaman tidak valid (dokumen hanya memiliki {max_pages} halaman).\n"
            f"Halaman mana yang mau diprint?\n"
            f"(Contoh: All, 1, 2, atau 1-3)"
        )
        return STEP_PAGES

    context.user_data["page_selection"] = teks if teks.lower() != "all" else f"All (1-{max_pages})"
    context.user_data["selected_page_list"] = selected_pages

    # Hitung total lembar & biaya
    copies = context.user_data["copies"]
    color_mode = context.user_data["color_mode"]
    total_sheets = len(selected_pages) * copies
    harga_satuan = HARGA_BW if color_mode == "B/W" else HARGA_COLOR
    total_biaya = total_sheets * harga_satuan

    # Data terstruktur siap kirim ke modul Pembayaran / CUPS Printer Raspberry Pi
    print_job_payload = {
        "user_id": update.effective_user.id,
        "file_id": context.user_data["file_id"],
        "file_name": context.user_data["file_name"],
        "color": color_mode,
        "copies": copies,
        "orientation": context.user_data["orientation"],
        "paper_size": context.user_data["paper_size"],
        "pages": context.user_data["page_selection"],
        "total_sheets": total_sheets,
        "total_price": total_biaya,
        "status": "READY_FOR_PAYMENT",
    }
    print("[LOG PRINT JOB UI/UX]:", print_job_payload)

    await update.message.reply_text(
        f"✅ **KONFIGURASI CETAK SELESAI**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 **Dokumen:** `{print_job_payload['file_name']}`\n"
        f"🎨 **Warna:** {print_job_payload['color']}\n"
        f"📑 **Jumlah Rangkap:** {print_job_payload['copies']}x\n"
        f"🔄 **Orientasi:** {print_job_payload['orientation']}\n"
        f"📏 **Ukuran Kertas:** {print_job_payload['paper_size']}\n"
        f"🔢 **Halaman Dicetak:** {print_job_payload['pages']} ({total_sheets} lembar)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 **Total Biaya:** **Rp {total_biaya:,}**\n\n"
        f"_(Siap dilanjutkan ke pembayaran QRIS & perintah cetak Raspberry Pi)_",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# Fallback jika user kirim teks di luar sesi
async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Silakan kirimkan **Dokumen PDF** untuk memulai proses cetak.", parse_mode="Markdown")


if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL, receive_pdf)],
        states={
            STEP_COLOR: [CallbackQueryHandler(select_color, pattern="^color_")],
            STEP_COPIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_copies)],
            STEP_ORIENTATION: [CallbackQueryHandler(select_orientation, pattern="^orient_")],
            STEP_PAPER: [CallbackQueryHandler(select_paper, pattern="^paper_")],
            STEP_PAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_pages)],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Document.ALL, receive_pdf),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    print("Bot VendingPrint (UI/UX Flow) sedang berjalan... Tekan Ctrl+C untuk berhenti.")
    app.run_polling()