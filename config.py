"""Konfigurasi terpusat VendingPrint — semua nilai dibaca dari file .env"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# ── Telegram ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ── Mode development ────────────────────────────────────────
# true  = walau printer tidak terhubung, user tetap bisa lanjut bayar
#         dan perintah cetak hanya DISIMULASIKAN (dicatat di log).
# false = mode produksi: printer tidak terhubung -> bot menolak order.
DEV_MODE = _bool("DEV_MODE", False)

# ── Printer / CUPS ──────────────────────────────────────────
# Kosongkan untuk otomatis memakai printer default CUPS
# (atau printer aktif pertama kalau default belum diset).
PRINTER_NAME = (os.getenv("PRINTER_NAME") or "").strip() or None

# ── Midtrans ────────────────────────────────────────────────
MIDTRANS_SERVER_KEY = os.getenv("MIDTRANS_SERVER_KEY")
MIDTRANS_IS_PRODUCTION = _bool("MIDTRANS_IS_PRODUCTION", False)
QRIS_ACQUIRER = os.getenv("QRIS_ACQUIRER", "gopay")
PAYMENT_EXPIRY_MINUTES = _int("PAYMENT_EXPIRY_MINUTES", 10)
PAYMENT_POLL_INTERVAL = _int("PAYMENT_POLL_INTERVAL", 5)  # detik

# ── Harga & operasional ─────────────────────────────────────
HARGA_BW = _int("HARGA_BW", 500)          # Rp / halaman
HARGA_COLOR = _int("HARGA_COLOR", 1500)   # Rp / halaman
CS_CONTACT = os.getenv("CS_CONTACT", "admin VendingPrint")
# Dipakai di gambar QRIS kalau nama merchant/NMID tidak terbaca dari kode QR
MERCHANT_NAME = os.getenv("MERCHANT_NAME", "VendingPrint")
MERCHANT_NMID = os.getenv("MERCHANT_NMID") or None
ADMIN_IDS = {
    int(x) for x in (os.getenv("ADMIN_IDS") or "").split(",") if x.strip().isdigit()
}

# Folder penyimpanan sementara PDF yang menunggu pembayaran/cetak
JOBS_DIR = BASE_DIR / "print_jobs"
JOBS_DIR.mkdir(exist_ok=True)


def validate():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN belum dikonfigurasi di file .env")
    if not MIDTRANS_SERVER_KEY:
        raise ValueError("MIDTRANS_SERVER_KEY belum dikonfigurasi di file .env")
    if MIDTRANS_IS_PRODUCTION and MIDTRANS_SERVER_KEY.startswith("SB-"):
        raise ValueError("MIDTRANS_IS_PRODUCTION=true tapi Server Key masih Sandbox (SB-...)")
    if not MIDTRANS_IS_PRODUCTION and not MIDTRANS_SERVER_KEY.startswith("SB-"):
        raise ValueError("MIDTRANS_IS_PRODUCTION=false tapi Server Key bukan Sandbox (harus diawali SB-)")
