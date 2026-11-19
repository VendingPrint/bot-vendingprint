"""
Membuat gambar tagihan QRIS bergaya VendingPrint.

Logo dibaca dari root folder proyek (folder yang sama dengan bot.py):
    logo-vp.png     -> logo VendingPrint (kiri atas)
    logo-itera.png  -> logo kampus ITERA (kanan atas)
Kalau file belum ada, posisi logo diisi teks pengganti.
Warna bisa diatur lewat .env: BRAND_COLOR dan ACCENT_COLOR (format hex, misal #1F4E9C).
"""
import io
import os
from pathlib import Path
from typing import Optional

import qrcode
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).resolve().parent
LOGO_VP = BASE_DIR / "logo-vp.png"
LOGO_ITERA = BASE_DIR / "logo-itera.png"

W, H = 1200, 1780


def _hex(value: str, default: tuple) -> tuple:
    try:
        v = value.lstrip("#")
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


BRAND = _hex(os.getenv("BRAND_COLOR", ""), (31, 78, 156))     # biru tua
ACCENT = _hex(os.getenv("ACCENT_COLOR", ""), (247, 148, 29))  # oranye
DARK = (33, 37, 41)
GRAY = (108, 117, 125)
LIGHT = (241, 244, 249)

_FONT_CANDIDATES = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ],
}


def _font(kind: str, size: int):
    custom = BASE_DIR / "assets" / f"font_{kind}.ttf"   # opsional: font sendiri
    for path in [custom, *_FONT_CANDIDATES[kind]]:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


# ── Membaca data merchant dari isi QRIS (format EMVCo) ──────
def _parse_tlv(data: str) -> dict:
    out, i = {}, 0
    while i + 4 <= len(data):
        tag, length = data[i:i + 2], data[i + 2:i + 4]
        if not length.isdigit():
            break
        n = int(length)
        out[tag] = data[i + 4:i + 4 + n]
        i += 4 + n
    return out


def parse_qris(qr_string: str) -> dict:
    """Ambil nama merchant (tag 59), kota (60), NMID dan TID dari string QRIS."""
    info = {"merchant_name": None, "city": None, "nmid": None, "tid": None}
    if not qr_string:
        return info
    try:
        tlv = _parse_tlv(qr_string)
        info["merchant_name"] = tlv.get("59")
        info["city"] = tlv.get("60")
        for tag in sorted(tlv, reverse=True):
            if tag.isdigit() and 26 <= int(tag) <= 51:
                sub = _parse_tlv(tlv[tag])
                if sub.get("02", "").startswith("ID"):
                    info["nmid"] = sub["02"]
                    break
        if "62" in tlv:
            info["tid"] = _parse_tlv(tlv["62"]).get("07")
    except Exception:
        pass
    return info


# ── Helper gambar ───────────────────────────────────────────
def _center_text(d, y, text, font, fill=DARK, max_width=W - 200, x_center=W / 2):
    while d.textlength(text, font=font) > max_width and font.size > 18:
        font = font.font_variant(size=font.size - 2)
    w = d.textlength(text, font=font)
    d.text((x_center - w / 2, y), text, font=font, fill=fill)
    top, bottom = font.getbbox("Hg")[1], font.getbbox("Hg")[3]
    return y + (bottom - top)


def _paste_logo(img, path: Path, box, fallback_text: str, align: str):
    """Tempel logo agar muat di dalam box (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    if path.exists():
        try:
            logo = Image.open(path).convert("RGBA")
            logo.thumbnail((x1 - x0, y1 - y0), Image.LANCZOS)
            lx = x0 if align == "left" else x1 - logo.width
            ly = y0 + (y1 - y0 - logo.height) // 2
            img.paste(logo, (lx, ly), logo)
            return
        except Exception:
            pass
    d = ImageDraw.Draw(img)
    f = _font("bold", 40)
    tw = d.textlength(fallback_text, font=f)
    tx = x0 if align == "left" else x1 - tw
    d.text((tx, y0 + (y1 - y0 - 40) / 2), fallback_text, font=f, fill=BRAND)


def _dashed_line(d, y, x0, x1, color=(206, 212, 218), dash=18, gap=12, width=3):
    x = x0
    while x < x1:
        d.line([x, y, min(x + dash, x1), y], fill=color, width=width)
        x += dash + gap


# ── Fungsi utama ────────────────────────────────────────────
def render_qris_card(qr_string: str, amount: int, order_id: str,
                     merchant_name: Optional[str] = None, nmid: Optional[str] = None,
                     detail: Optional[str] = None, expiry_minutes: Optional[int] = None,
                     **_ignored) -> io.BytesIO:
    """Return gambar PNG (BytesIO) siap dikirim via reply_photo."""
    parsed = parse_qris(qr_string)
    merchant_name = parsed["merchant_name"] or merchant_name or "VendingPrint"
    nmid = parsed["nmid"] or nmid

    img = Image.new("RGB", (W, H), LIGHT)
    d = ImageDraw.Draw(img)

    # Pita warna di atas + aksen
    d.rectangle([0, 0, W, 420], fill=BRAND)
    d.ellipse([W - 330, -200, W + 170, 300], fill=tuple(min(255, c + 25) for c in BRAND))
    d.ellipse([-180, 250, 160, 590], fill=tuple(min(255, c + 18) for c in BRAND))

    # Kartu putih utama
    card = (70, 70, W - 70, H - 150)
    d.rounded_rectangle(card, radius=44, fill="white")

    # Header logo (VendingPrint kiri, ITERA kanan) + garis pemisah
    _paste_logo(img, LOGO_VP, (130, 110, 520, 230), "VendingPrint", "left")
    _paste_logo(img, LOGO_ITERA, (W - 380, 100, W - 130, 240), "ITERA", "right")
    d.line([130, 275, W - 130, 275], fill=(233, 236, 239), width=3)

    # Judul
    y = 310
    y = _center_text(d, y, "PEMBAYARAN QRIS", _font("bold", 54), fill=BRAND) + 22
    y = _center_text(d, y, merchant_name, _font("regular", 36), fill=DARK) + 12
    if nmid:
        y = _center_text(d, y, f"NMID: {nmid}", _font("regular", 28), fill=GRAY) + 8

    # QR code dalam bingkai
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=1, box_size=10)
    qr.add_data(qr_string)
    qr.make(fit=True)
    qr_size = 560
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
    qx, qy = (W - qr_size) // 2, y + 64
    d.rounded_rectangle([qx - 34, qy - 34, qx + qr_size + 34, qy + qr_size + 34],
                        radius=30, outline=ACCENT, width=8, fill="white")
    img.paste(qr_img, (qx, qy))

    # Total bayar (kotak warna)
    y = qy + qr_size + 80
    d.rounded_rectangle([200, y, W - 200, y + 150], radius=26, fill=LIGHT)
    _center_text(d, y + 20, "Total Bayar", _font("regular", 32), fill=GRAY)
    total = f"Rp {amount:,}".replace(",", ".")
    _center_text(d, y + 62, total, _font("bold", 64), fill=BRAND)
    y += 185

    if detail:
        y = _center_text(d, y, detail, _font("regular", 32), fill=DARK) + 14
    if expiry_minutes:
        y = _center_text(d, y, f"Berlaku {expiry_minutes} menit",
                         _font("bold", 30), fill=ACCENT) + 14

    # Garis putus-putus (gaya struk) + info order
    _dashed_line(d, card[3] - 140, 130, W - 130)
    _center_text(d, card[3] - 110, f"Order ID: {order_id}", _font("regular", 30), fill=DARK)
    _center_text(d, card[3] - 65, "Scan dengan e-wallet / m-banking yang mendukung QRIS",
                 _font("regular", 26), fill=GRAY)

    # Footer di luar kartu
    _center_text(d, H - 105, "VendingPrint  •  Institut Teknologi Sumatera",
                 _font("bold", 30), fill=BRAND)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
