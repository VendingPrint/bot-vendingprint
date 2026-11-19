"""
Modul deteksi & cetak via CUPS (memakai perintah lpstat / lpinfo / lp).

Kenapa perlu lpinfo? Antrean printer di CUPS tetap berstatus "idle" walaupun
kabel USB-nya dicabut. Jadi untuk printer USB, kita cocokkan URI antrean
dengan daftar perangkat USB yang benar-benar terpasang (lpinfo -v).
Untuk printer jaringan (ipp://, socket://, dll) kita coba buka koneksi TCP.
"""
import logging
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, unquote

log = logging.getLogger(__name__)

# Paksa output CUPS berbahasa Inggris supaya parsing konsisten
_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}

_DEFAULT_PORTS = {"ipp": 631, "ipps": 631, "http": 631, "https": 443, "socket": 9100, "lpd": 515}


class CupsError(Exception):
    pass


@dataclass
class PrinterInfo:
    name: str
    enabled: bool
    state: str                       # idle / printing / disabled
    uri: str = ""
    is_default: bool = False
    connected: Optional[bool] = None  # None = tidak bisa dipastikan


@dataclass
class PrinterStatus:
    ok: bool
    message: str
    printer: Optional[PrinterInfo] = None
    printers: list = field(default_factory=list)
    scheduler_running: bool = False


def _run(args, timeout=15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=_ENV)
    except FileNotFoundError:
        raise CupsError(f"Perintah '{args[0]}' tidak ditemukan — CUPS belum terinstal?")
    except subprocess.TimeoutExpired:
        raise CupsError(f"Perintah '{' '.join(args)}' timeout")


# ── Membaca konfigurasi CUPS ────────────────────────────────
def _scheduler_running() -> bool:
    r = _run(["lpstat", "-r"])
    return "scheduler is running" in r.stdout


def _list_printers() -> list[PrinterInfo]:
    printers: dict[str, PrinterInfo] = {}

    # Status antrean: "printer HP_DeskJet is idle.  enabled since ..."
    r = _run(["lpstat", "-p"])
    for line in r.stdout.splitlines():
        m = re.match(r"^printer\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        name, rest = m.groups()
        if "disabled" in rest:
            state, enabled = "disabled", False
        elif "printing" in rest:
            state, enabled = "printing", True
        else:
            state, enabled = "idle", True
        printers[name] = PrinterInfo(name=name, enabled=enabled, state=state)

    # URI perangkat: "device for HP_DeskJet: usb://HP/DeskJet?serial=..."
    r = _run(["lpstat", "-v"])
    for line in r.stdout.splitlines():
        m = re.match(r"^device for\s+(\S+):\s+(\S+)", line)
        if m and m.group(1) in printers:
            printers[m.group(1)].uri = m.group(2)

    # Printer default: "system default destination: HP_DeskJet"
    r = _run(["lpstat", "-d"])
    m = re.search(r"system default destination:\s+(\S+)", r.stdout)
    if m and m.group(1) in printers:
        printers[m.group(1)].is_default = True

    return list(printers.values())


def _attached_usb_uris() -> Optional[set[str]]:
    """Daftar URI USB yang sedang tertancap. None = tidak bisa dicek (misal izin)."""
    try:
        r = _run(["lpinfo", "--include-schemes", "usb", "-v"], timeout=30)
    except CupsError as e:
        log.warning("lpinfo gagal: %s", e)
        return None
    if r.returncode != 0:
        log.warning("lpinfo gagal (user perlu masuk grup lpadmin?): %s", r.stderr.strip())
        return None
    uris = set()
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].startswith("usb://"):
            uris.add(parts[1].strip())
    return uris


def _usb_key(uri: str) -> str:
    """Bandingkan URI USB tanpa query (?serial=...) dan tanpa beda encoding."""
    return unquote(uri.split("?", 1)[0]).lower()


def _check_connected(p: PrinterInfo) -> Optional[bool]:
    scheme = p.uri.split(":", 1)[0].lower() if p.uri else ""

    if scheme == "usb":
        attached = _attached_usb_uris()
        if attached is None:
            return None
        if p.uri in attached:
            return True
        return _usb_key(p.uri) in {_usb_key(u) for u in attached}

    if scheme in _DEFAULT_PORTS:
        parsed = urlparse(p.uri)
        host = parsed.hostname
        if not host:
            return None
        port = parsed.port or _DEFAULT_PORTS[scheme]
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            return False

    # dnssd://, implicitclass://, dll. -> tidak dicek, anggap mengikuti status antrean
    return None


def _choose(printers: list[PrinterInfo], preferred: Optional[str]) -> Optional[PrinterInfo]:
    if preferred:
        return next((p for p in printers if p.name == preferred), None)
    return (
        next((p for p in printers if p.is_default), None)
        or next((p for p in printers if p.enabled), None)
        or (printers[0] if printers else None)
    )


# ── Fungsi utama yang dipanggil bot ─────────────────────────
def check_printer(preferred_name: Optional[str] = None) -> PrinterStatus:
    """Cek apakah ada printer CUPS yang siap dipakai. Fungsi ini BLOCKING
    (panggil lewat asyncio.to_thread dari kode async)."""
    try:
        if not _scheduler_running():
            return PrinterStatus(False, "Layanan CUPS (cupsd) tidak berjalan")

        printers = _list_printers()
        if not printers:
            return PrinterStatus(False, "Belum ada printer yang terdaftar di CUPS", scheduler_running=True)

        p = _choose(printers, preferred_name)
        if p is None:
            names = ", ".join(x.name for x in printers)
            return PrinterStatus(False, f"Printer '{preferred_name}' tidak ada di CUPS (tersedia: {names})",
                                 printers=printers, scheduler_running=True)

        if not p.enabled:
            return PrinterStatus(False, f"Printer '{p.name}' berstatus disabled di CUPS",
                                 p, printers, True)

        p.connected = _check_connected(p)
        if p.connected is False:
            return PrinterStatus(False, f"Printer '{p.name}' terdaftar di CUPS tapi perangkatnya tidak terhubung",
                                 p, printers, True)

        note = "" if p.connected else " (koneksi fisik tidak bisa diverifikasi)"
        return PrinterStatus(True, f"Printer '{p.name}' siap{note}", p, printers, True)

    except CupsError as e:
        return PrinterStatus(False, str(e))


def pages_to_ranges(pages: list[int]) -> str:
    """[1,2,3,5,7,8] -> '1-3,5,7-8' (format page-ranges CUPS)"""
    pages = sorted(set(pages))
    if not pages:
        return ""
    ranges, start, prev = [], pages[0], pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = p
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def build_lp_command(printer_name: str, file_path: str, copies: int, pages: list[int],
                     color_mode: str, orientation: str, paper_size: str, title: str) -> list[str]:
    args = [
        "lp", "-d", printer_name,
        "-n", str(copies),
        "-t", title[:60],
        "-o", f"media={paper_size}",
        "-o", f"page-ranges={pages_to_ranges(pages)}",
        "-o", f"orientation-requested={'4' if orientation.lower() == 'landscape' else '3'}",
        "-o", "fit-to-page",
        "-o", "collate=true",
    ]
    if color_mode == "B/W":
        # print-color-mode untuk driver IPP Everywhere, ColorModel untuk driver PPD
        args += ["-o", "print-color-mode=monochrome", "-o", "ColorModel=Gray"]
    else:
        args += ["-o", "print-color-mode=color"]
    args.append(str(file_path))
    return args


def print_file(printer_name: str, file_path: str, copies: int, pages: list[int],
               color_mode: str, orientation: str, paper_size: str, title: str) -> str:
    """Kirim dokumen ke CUPS. Return Job ID CUPS. Fungsi ini BLOCKING."""
    args = build_lp_command(printer_name, file_path, copies, pages, color_mode, orientation, paper_size, title)
    log.info("Menjalankan: %s", " ".join(args))
    r = _run(args, timeout=60)
    if r.returncode != 0:
        raise CupsError(r.stderr.strip() or r.stdout.strip() or "lp gagal tanpa pesan")
    m = re.search(r"request id is (\S+)", r.stdout)
    return m.group(1) if m else r.stdout.strip()
