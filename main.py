import asyncio
import html
import logging
import time
from pathlib import Path

from pypdf import PdfReader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

import config
from printer import check_printer, print_file, build_lp_command, CupsError, PrinterStatus
from qris_card import render_qris_card
from payment import MidtransClient, MidtransError, PAID_STATUSES, FAILED_STATUSES

config.validate()

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("vendingprint")

midtrans = MidtransClient(config.MIDTRANS_SERVER_KEY, config.MIDTRANS_IS_PRODUCTION)

# Tahapan (States) alur percakapan sesuai wireframe UI/UX
STEP_COLOR, STEP_COPIES, STEP_ORIENTATION, STEP_PAPER, STEP_PAGES, STEP_CONFIRM = range(6)

MAX_FILE_SIZE = 20 * 1024 * 1024  # batas download file oleh Bot API Telegram
HTML = ParseMode.HTML


def esc(text) -> str:
    return html.escape(str(text))


def rupiah(n: int) -> str:
    return f"Rp {n:,}".replace(",", ".")


def msg_mesin_off() -> str:
    return (
        "⛔ <b>Mesin sedang tidak aktif.</b>\n"
        f"Tunggu beberapa saat atau kontak CS: {esc(config.CS_CONTACT)}"
    )


def jobs_store(app: Application) -> dict:
    return app.bot_data.setdefault("jobs", {})


def delete_file(path):
    try:
        if path and Path(path).exists():
            Path(path).unlink()
    except OSError as e:
        log.warning("Gagal menghapus %s: %s", path, e)


def cleanup_draft(context: ContextTypes.DEFAULT_TYPE):
    """Hapus PDF yang sudah diunggah tapi belum dibuatkan order pembayaran."""
    delete_file(context.user_data.get("local_path"))
    context.user_data.clear()


# ── Cek printer ──────────────────────────────────────────────
async def get_printer_status() -> PrinterStatus:
    return await asyncio.to_thread(check_printer, config.PRINTER_NAME)


async def ensure_printer_ready(message) -> bool:
    """True = boleh lanjut. Mode produksi: tolak kalau printer tidak siap.
    Mode development: tetap lanjut dengan peringatan."""
    status = await get_printer_status()
    if status.ok:
        return True

    log.warning("Printer tidak siap: %s", status.message)
    if config.DEV_MODE:
        await message.reply_text(
            "🛠 <b>MODE DEVELOPMENT</b>\n"
            f"Printer tidak terdeteksi (<i>{esc(status.message)}</i>).\n"
            "Proses tetap dilanjutkan; perintah cetak hanya disimulasikan.",
            parse_mode=HTML,
        )
        return True

    await message.reply_text(msg_mesin_off(), parse_mode=HTML)
    return False


# Fungsi validasi rentang halaman ("All", "1", "2", "1-3", atau "1,3,5-7")
def parse_page_selection(text: str, max_pages: int):
    clean = text.strip().lower()
    if clean == "all":
        return list(range(1, max_pages + 1))

    pages = set()
    for part in clean.replace(" ", "").split(","):
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


# ── Command /start ───────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_draft(context)
    if not await ensure_printer_ready(update.message):
        return ConversationHandler.END

    user_name = esc(update.effective_user.first_name)
    await update.message.reply_text(
        f"Halo {user_name}! Selamat datang di <b>Bot VendingPrint</b>.\n"
        f"Silakan kirim <b>Dokumen PDF</b> yang ingin dicetak.",
        parse_mode=HTML,
    )
    return ConversationHandler.END


# ── Command /batal ───────────────────────────────────────────
async def batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_draft(context)
    await update.message.reply_text("Konfigurasi cetak dibatalkan. Kirim PDF baru kapan saja.")
    return ConversationHandler.END


# ── Command /printer (cek status printer & nama di CUPS) ─────
async def printer_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if config.ADMIN_IDS and update.effective_user.id not in config.ADMIN_IDS:
        return
    status = await get_printer_status()
    lines = [
        "<b>🖨 Status Printer (CUPS)</b>",
        f"Mode: {'🛠 DEVELOPMENT' if config.DEV_MODE else '🟢 PRODUKSI'}",
        f"Midtrans: {'PRODUCTION' if config.MIDTRANS_IS_PRODUCTION else 'SANDBOX'}",
        f"CUPS scheduler: {'berjalan' if status.scheduler_running else 'tidak berjalan'}",
        f"Hasil cek: {'✅' if status.ok else '❌'} {esc(status.message)}",
    ]
    if status.printers:
        lines.append("\n<b>Printer terdaftar:</b>")
        for p in status.printers:
            mark = "👉 " if status.printer and p.name == status.printer.name else "• "
            default = " (default)" if p.is_default else ""
            lines.append(f"{mark}<code>{esc(p.name)}</code>{default} — {p.state}\n   <code>{esc(p.uri or '-')}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


# 1. Menerima Dokumen PDF -> Step 1 (Atur warna dokumen)
async def receive_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_draft(context)

    # Cek mesin dulu sebelum user mengisi konfigurasi
    if not await ensure_printer_ready(update.message):
        return ConversationHandler.END

    doc = update.message.document
    file_name = doc.file_name or "dokumen.pdf"

    if not file_name.lower().endswith(".pdf") and doc.mime_type != "application/pdf":
        await update.message.reply_text("Berkas ditolak! Mohon kirimkan dokumen berformat PDF.")
        return ConversationHandler.END

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("Ukuran file maksimal 20 MB. Silakan kompres PDF lalu kirim ulang.")
        return ConversationHandler.END

    local_path = config.JOBS_DIR / f"{doc.file_unique_id}_{int(time.time())}.pdf"
    try:
        telegram_file = await context.bot.get_file(doc.file_id)
        await telegram_file.download_to_drive(local_path)
        total_pages = len(PdfReader(local_path).pages)
    except Exception:
        log.exception("Gagal mengunduh/membaca PDF")
        delete_file(local_path)
        await update.message.reply_text("Gagal membaca file PDF. Pastikan file tidak rusak atau terkunci.")
        return ConversationHandler.END

    # File DISIMPAN (tidak dihapus) karena dibutuhkan saat mencetak setelah bayar
    context.user_data.update(
        file_id=doc.file_id,
        file_name=file_name,
        total_doc_pages=total_pages,
        local_path=str(local_path),
    )

    keyboard = [[
        InlineKeyboardButton("B/W", callback_data="color_bw"),
        InlineKeyboardButton("Color", callback_data="color_full"),
    ]]
    await update.message.reply_text(
        f"📄 Dokumen diterima: <code>{esc(file_name)}</code> ({total_pages} halaman)\n\n"
        f"Atur warna dokumen",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=HTML,
    )
    return STEP_COLOR


# 2. Pilihan Warna -> tanya jumlah rangkap
async def select_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pilihan = "B/W" if query.data == "color_bw" else "Color"
    context.user_data["color_mode"] = pilihan

    await query.edit_message_text(f"Atur warna dokumen: <b>{pilihan}</b> ✅", parse_mode=HTML)
    await query.message.reply_text("Berapa rangkap yang ingin dicetak? (contoh: 1)")
    return STEP_COPIES


# 3. Jumlah rangkap -> tanya Portrait / Landscape
async def input_copies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text.strip()
    if not teks.isdigit() or not (1 <= int(teks) <= 50):
        await update.message.reply_text("Mohon masukkan angka 1–50, contoh: <code>1</code>", parse_mode=HTML)
        return STEP_COPIES

    context.user_data["copies"] = int(teks)
    keyboard = [[
        InlineKeyboardButton("Portrait", callback_data="orient_Portrait"),
        InlineKeyboardButton("Landscape", callback_data="orient_Landscape"),
    ]]
    await update.message.reply_text("Portrait atau landscape?", reply_markup=InlineKeyboardMarkup(keyboard))
    return STEP_ORIENTATION


# 4. Orientasi -> tanya Ukuran Kertas (hanya A4 aktif)
async def select_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    orientasi = query.data.replace("orient_", "")
    context.user_data["orientation"] = orientasi
    await query.edit_message_text(f"Portrait atau landscape?: <b>{orientasi}</b> ✅", parse_mode=HTML)

    keyboard = [[
        InlineKeyboardButton("F4 (Nonaktif)", callback_data="paper_disabled"),
        InlineKeyboardButton("A5 (Nonaktif)", callback_data="paper_disabled"),
        InlineKeyboardButton("A4", callback_data="paper_A4"),
    ]]
    await query.message.reply_text(
        "Ukuran kertas?\n(untuk sekarang hanya bisa A4)",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return STEP_PAPER


# 5. Ukuran Kertas -> tanya halaman
async def select_paper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "paper_disabled":
        await query.answer("Maaf, untuk sekarang ukuran kertas hanya tersedia A4.", show_alert=True)
        return STEP_PAPER

    await query.answer()
    context.user_data["paper_size"] = "A4"
    await query.edit_message_text("Ukuran kertas: <b>A4</b> ✅", parse_mode=HTML)
    await query.message.reply_text("Halaman mana yang mau diprint?\n(Contoh: All, 1, 2, 1-3, atau 1,3,5-7)")
    return STEP_PAGES


# 6. Pilihan Halaman -> Ringkasan + tombol Bayar
async def input_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text.strip()
    max_pages = context.user_data.get("total_doc_pages", 1)

    selected_pages = parse_page_selection(teks, max_pages)
    if not selected_pages:
        await update.message.reply_text(
            f"⚠️ Input halaman tidak valid (dokumen hanya memiliki {max_pages} halaman).\n"
            f"Halaman mana yang mau diprint?\n(Contoh: All, 1, 2, atau 1-3)"
        )
        return STEP_PAGES

    ud = context.user_data
    copies = ud["copies"]
    color_mode = ud["color_mode"]
    total_sheets = len(selected_pages) * copies
    harga_satuan = config.HARGA_BW if color_mode == "B/W" else config.HARGA_COLOR
    total_biaya = total_sheets * harga_satuan

    ud["draft"] = {
        "user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "file_id": ud["file_id"],
        "file_name": ud["file_name"],
        "local_path": ud["local_path"],
        "color": color_mode,
        "copies": copies,
        "orientation": ud["orientation"],
        "paper_size": ud["paper_size"],
        "pages": teks if teks.lower() != "all" else f"All (1-{max_pages})",
        "page_list": selected_pages,
        "total_sheets": total_sheets,
        "unit_price": harga_satuan,
        "total_price": total_biaya,
        "status": "READY_FOR_PAYMENT",
    }
    d = ud["draft"]
    log.info("[DRAFT PRINT JOB] %s", {k: v for k, v in d.items() if k != "page_list"})

    keyboard = [[
        InlineKeyboardButton("💳 Bayar dengan QRIS", callback_data="pay_qris"),
        InlineKeyboardButton("❌ Batal", callback_data="pay_cancel"),
    ]]
    await update.message.reply_text(
        f"✅ <b>KONFIGURASI CETAK SELESAI</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>Dokumen:</b> <code>{esc(d['file_name'])}</code>\n"
        f"🎨 <b>Warna:</b> {d['color']}\n"
        f"📑 <b>Jumlah Rangkap:</b> {d['copies']}x\n"
        f"🔄 <b>Orientasi:</b> {d['orientation']}\n"
        f"📏 <b>Ukuran Kertas:</b> {d['paper_size']}\n"
        f"🔢 <b>Halaman Dicetak:</b> {esc(d['pages'])} ({total_sheets} lembar)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Total Biaya: {rupiah(total_biaya)}</b>\n\n"
        f"Lanjutkan ke pembayaran?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=HTML,
    )
    return STEP_CONFIRM


# 7. Tombol Bayar / Batal -> buat transaksi QRIS Midtrans
async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    draft = context.user_data.get("draft")

    if query.data == "pay_cancel" or not draft:
        await query.answer()
        cleanup_draft(context)
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("Pesanan dibatalkan. Kirim PDF baru kapan saja.")
        return ConversationHandler.END

    await query.answer("Menyiapkan QRIS...")

    # Cek ulang printer sebelum user membayar
    if not await ensure_printer_ready(query.message):
        return STEP_CONFIRM  # user bisa tekan "Bayar" lagi setelah mesin aktif

    order_id = f"VP-{draft['user_id']}-{int(time.time())}"
    item_name = f"Cetak {draft['color']} {draft['paper_size']}"
    try:
        charge = await midtrans.create_qris_charge(
            order_id=order_id,
            gross_amount=draft["total_price"],
            items=[{
                "id": "print-" + ("bw" if draft["color"] == "B/W" else "color"),
                "price": draft["unit_price"],
                "quantity": draft["total_sheets"],
                "name": item_name,
            }],
            expiry_minutes=config.PAYMENT_EXPIRY_MINUTES,
            acquirer=config.QRIS_ACQUIRER,
            customer={"first_name": (query.from_user.first_name or "User")[:50]},
        )
    except MidtransError as e:
        log.error("Gagal membuat QRIS %s: %s", order_id, e)
        await query.message.reply_text(
            "⚠️ Gagal membuat kode pembayaran. Silakan tekan tombol <b>Bayar</b> lagi "
            f"atau kontak CS: {esc(config.CS_CONTACT)}",
            parse_mode=HTML,
        )
        return STEP_CONFIRM

    await query.edit_message_reply_markup(None)

    caption = (
        f"🧾 <b>Pembayaran QRIS</b>\n"
        f"Order ID: <code>{order_id}</code>\n"
        f"Total: <b>{rupiah(draft['total_price'])}</b>\n\n"
        f"Scan QR di atas dengan aplikasi e-wallet / m-banking yang mendukung QRIS.\n"
        f"⏳ Berlaku {config.PAYMENT_EXPIRY_MINUTES} menit. Dokumen otomatis dicetak setelah pembayaran diterima."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Cek Status", callback_data=f"chk:{order_id}"),
        InlineKeyboardButton("❌ Batalkan", callback_data=f"cxl:{order_id}"),
    ]])

    if charge["qr_string"]:
        # Gambar tagihan QRIS: logo VendingPrint & ITERA, QR, total bayar, Order ID
        photo = await asyncio.to_thread(
            render_qris_card,
            charge["qr_string"], draft["total_price"], order_id,
            merchant_name=config.MERCHANT_NAME, nmid=config.MERCHANT_NMID,
            detail=f"{draft['total_sheets']} lembar • {draft['color']} • {draft['paper_size']}",
            expiry_minutes=config.PAYMENT_EXPIRY_MINUTES,
        )
    else:
        photo = charge["qr_url"]
    qr_msg = await query.message.reply_photo(photo=photo, caption=caption, parse_mode=HTML, reply_markup=keyboard)

    # Khusus Sandbox: kirim link gambar QR supaya bisa ditempel di simulator Midtrans
    if not config.MIDTRANS_IS_PRODUCTION:
        await send_sandbox_qr_link(query.message, charge)

    # Pindahkan draft ke penyimpanan order (bot_data), file sekarang "milik" order ini
    job = {**draft, "order_id": order_id, "status": "PENDING_PAYMENT",
           "transaction_id": charge["transaction_id"], "qr_message_id": qr_msg.message_id,
           "created_at": time.time()}
    jobs_store(context.application)[order_id] = job
    context.user_data.clear()
    log.info("[ORDER DIBUAT] %s %s", order_id, rupiah(job["total_price"]))

    context.application.create_task(poll_payment(context.application, order_id), name=f"poll-{order_id}")
    return ConversationHandler.END


SANDBOX_SIMULATOR_URL = "https://simulator.sandbox.midtrans.com/v2/qris/index"


async def send_sandbox_qr_link(message, charge: dict):
    qr_url = charge.get("qr_url")
    if not qr_url:
        log.warning("Respons Midtrans tidak berisi URL generate-qr-code: %s", charge.get("raw"))
        await message.reply_text("🧪 [SANDBOX] URL gambar QR tidak tersedia dari Midtrans (cek log).")
        return
    log.info("[SANDBOX] QR image URL: %s", qr_url)
    await message.reply_text(
        "🧪 <b>MODE SANDBOX</b> — untuk tes bayar:\n"
        "1. Salin link gambar QR di bawah ini\n"
        "2. Buka simulator, tempel link-nya di kolom <i>QR Code Image URL</i>, lalu bayar\n\n"
        f"<code>{esc(qr_url)}</code>",
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🧪 Buka Simulator QRIS", url=SANDBOX_SIMULATOR_URL),
        ]]),
    )


# ── Pemantauan pembayaran ────────────────────────────────────
async def poll_payment(app: Application, order_id: str):
    deadline = time.monotonic() + (config.PAYMENT_EXPIRY_MINUTES + 2) * 60
    while time.monotonic() < deadline:
        await asyncio.sleep(config.PAYMENT_POLL_INTERVAL)
        job = jobs_store(app).get(order_id)
        if not job or job["status"] != "PENDING_PAYMENT":
            return
        try:
            data = await midtrans.get_status(order_id)
        except MidtransError as e:
            log.warning("Cek status %s gagal: %s", order_id, e)
            continue
        if await handle_transaction_status(app, order_id, data):
            return

    # Batas waktu habis tanpa status final dari Midtrans
    job = jobs_store(app).get(order_id)
    if job and job["status"] == "PENDING_PAYMENT":
        await finish_unpaid(app, job, "expire")


async def handle_transaction_status(app: Application, order_id: str, data: dict,
                                    user_cancelled: bool = False) -> bool:
    """Proses status dari Midtrans. Return True kalau transaksi sudah final."""
    job = jobs_store(app).get(order_id)
    if not job or job["status"] != "PENDING_PAYMENT":
        return True

    ts = data.get("transaction_status")
    if ts in PAID_STATUSES:
        try:
            paid = int(float(data.get("gross_amount", 0)))
        except (TypeError, ValueError):
            paid = -1
        if paid != job["total_price"]:
            job["status"] = "AMOUNT_MISMATCH"
            log.error("Nominal tidak cocok %s: dibayar %s, tagihan %s", order_id, paid, job["total_price"])
            await app.bot.send_message(
                job["chat_id"],
                f"⚠️ Nominal pembayaran tidak sesuai. Hubungi CS {esc(config.CS_CONTACT)} "
                f"dengan Order ID <code>{order_id}</code>.",
                parse_mode=HTML,
            )
            return True
        job["status"] = "PAID"  # diset sebelum await -> mencegah cetak dobel
        job["paid_at"] = time.time()
        await process_paid_job(app, job)
        return True

    if ts in FAILED_STATUSES:
        await finish_unpaid(app, job, "cancel" if user_cancelled else ts)
        return True

    return False  # masih pending


async def remove_qr_buttons(app: Application, job: dict):
    try:
        await app.bot.edit_message_reply_markup(job["chat_id"], job["qr_message_id"], reply_markup=None)
    except TelegramError:
        pass


async def finish_unpaid(app: Application, job: dict, reason: str):
    job["status"] = "EXPIRED" if reason == "expire" else "CANCELLED"
    delete_file(job["local_path"])
    await remove_qr_buttons(app, job)
    teks = ("⌛ Waktu pembayaran habis." if reason == "expire" else "❌ Pembayaran dibatalkan.")
    await app.bot.send_message(
        job["chat_id"],
        f"{teks}\nOrder <code>{job['order_id']}</code> tidak diproses. Kirim PDF lagi untuk memesan ulang.",
        parse_mode=HTML,
    )
    log.info("[ORDER %s] %s", job["status"], job["order_id"])


async def process_paid_job(app: Application, job: dict):
    bot, chat_id, order_id = app.bot, job["chat_id"], job["order_id"]
    await remove_qr_buttons(app, job)
    await bot.send_message(chat_id, f"✅ <b>Pembayaran diterima!</b>\nOrder <code>{order_id}</code>\nMenyiapkan cetak...",
                           parse_mode=HTML)

    status = await get_printer_status()
    title = f"{order_id} {job['file_name']}"

    if not status.ok:
        if config.DEV_MODE:
            cmd = build_lp_command("<PRINTER>", job["local_path"], job["copies"], job["page_list"],
                                   job["color"], job["orientation"], job["paper_size"], title)
            log.info("[SIMULASI CETAK] %s", " ".join(cmd))
            job["status"] = "PRINTED_SIMULATED"
            delete_file(job["local_path"])
            await bot.send_message(
                chat_id,
                "🛠 <b>MODE DEVELOPMENT</b>\nPrinter tidak terhubung, perintah cetak hanya disimulasikan "
                "(lihat log). Dokumen tidak benar-benar dicetak.",
                parse_mode=HTML,
            )
            return
        await report_print_failure(app, job, status.message)
        return

    try:
        cups_job_id = await asyncio.to_thread(
            print_file, status.printer.name, job["local_path"], job["copies"], job["page_list"],
            job["color"], job["orientation"], job["paper_size"], title,
        )
    except CupsError as e:
        await report_print_failure(app, job, str(e))
        return

    job.update(status="PRINTING", cups_job_id=cups_job_id, printer=status.printer.name)
    delete_file(job["local_path"])  # file sudah disalin ke spool CUPS
    log.info("[DICETAK] %s -> %s (%s)", order_id, status.printer.name, cups_job_id)
    await bot.send_message(
        chat_id,
        f"🖨 <b>Dokumen sedang dicetak!</b>\n"
        f"{job['total_sheets']} lembar • {job['color']} • {job['paper_size']}\n"
        f"Job: <code>{esc(cups_job_id)}</code>\n\nSilakan ambil hasil cetak di mesin. Terima kasih! 🙏",
        parse_mode=HTML,
    )


async def report_print_failure(app: Application, job: dict, reason: str):
    # Sudah bayar tapi gagal cetak -> file TIDAK dihapus supaya CS bisa cetak ulang manual
    job["status"] = "PAID_PRINT_FAILED"
    job["error"] = reason
    log.error("[GAGAL CETAK] %s: %s (file: %s)", job["order_id"], reason, job["local_path"])
    await app.bot.send_message(
        job["chat_id"],
        "⚠️ Pembayaran kamu <b>sudah diterima</b>, tetapi mesin sedang tidak aktif sehingga dokumen belum tercetak.\n"
        f"Simpan Order ID <code>{job['order_id']}</code> dan kontak CS: {esc(config.CS_CONTACT)}",
        parse_mode=HTML,
    )


# Tombol "Cek Status" & "Batalkan" pada pesan QR
async def payment_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, order_id = query.data.split(":", 1)
    job = jobs_store(context.application).get(order_id)

    if not job:
        await query.answer("Transaksi tidak ditemukan atau sudah kedaluwarsa.", show_alert=True)
        return
    if job["user_id"] != query.from_user.id:
        await query.answer("Ini bukan transaksi kamu.", show_alert=True)
        return
    if job["status"] != "PENDING_PAYMENT":
        await query.answer(f"Status transaksi: {job['status']}", show_alert=True)
        return

    try:
        if action == "cxl":
            await query.answer("Membatalkan...")
            try:
                await midtrans.expire(order_id)
            except MidtransError as e:
                log.warning("Expire %s gagal: %s", order_id, e)
            # Cek ulang: bisa jadi user sudah terlanjur bayar
            data = await midtrans.get_status(order_id)
            if not await handle_transaction_status(context.application, order_id, data, user_cancelled=True):
                await finish_unpaid(context.application, job, "cancel")
        else:
            data = await midtrans.get_status(order_id)
            if await handle_transaction_status(context.application, order_id, data):
                await query.answer()
            else:
                await query.answer("Belum ada pembayaran masuk. Scan QRIS lalu tunggu sebentar.", show_alert=True)
    except MidtransError as e:
        log.warning("payment_action %s gagal: %s", order_id, e)
        try:
            await query.answer("Gagal menghubungi server pembayaran, coba lagi.", show_alert=True)
        except TelegramError:
            pass


# Fallback jika user kirim teks di luar sesi
async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Silakan kirimkan <b>Dokumen PDF</b> untuk memulai proses cetak.", parse_mode=HTML)


async def on_startup(app: Application):
    # Hapus PDF sisa yang berumur > 1 hari
    for f in config.JOBS_DIR.glob("*.pdf"):
        if time.time() - f.stat().st_mtime > 86400:
            delete_file(f)

    status = await get_printer_status()
    log.info("Mode: %s | Midtrans: %s", "DEVELOPMENT" if config.DEV_MODE else "PRODUKSI",
             "PRODUCTION" if config.MIDTRANS_IS_PRODUCTION else "SANDBOX")
    if status.ok:
        log.info("✅ %s | URI: %s", status.message, status.printer.uri)
    else:
        log.warning("❌ Printer tidak siap: %s%s", status.message,
                    " (DEV_MODE aktif, bot tetap melayani)" if config.DEV_MODE else "")
    for p in status.printers:
        log.info("   CUPS printer: %s [%s]%s %s", p.name, p.state, " (default)" if p.is_default else "", p.uri)


if __name__ == "__main__":
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).post_init(on_startup).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL, receive_pdf)],
        states={
            STEP_COLOR: [CallbackQueryHandler(select_color, pattern="^color_")],
            STEP_COPIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_copies)],
            STEP_ORIENTATION: [CallbackQueryHandler(select_orientation, pattern="^orient_")],
            STEP_PAPER: [CallbackQueryHandler(select_paper, pattern="^paper_")],
            STEP_PAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_pages)],
            STEP_CONFIRM: [CallbackQueryHandler(confirm_payment, pattern="^pay_")],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("batal", batal),
            MessageHandler(filters.Document.ALL, receive_pdf),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("printer", printer_info))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(payment_action, pattern=r"^(chk|cxl):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    print("Bot VendingPrint sedang berjalan... Tekan Ctrl+C untuk berhenti.")
    app.run_polling()
