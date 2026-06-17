#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UT Ruang Baca Virtual (RBV) - Auto Downloader Buku ke PDF
=========================================================

Alur kerja:
1. Login ke https://pustaka.ut.ac.id/reader/index.php
   - Masukkan username & password
   - Captcha berbasis teks (mis. "Berapa hasil dari 6 + 8 =") diselesaikan
     manual di terminal (script akan menampilkan soalnya).
2. Setelah login -> dashboard rbv.php.
   - Pilih menu: [1] Terakhir Dibaca  atau  [2] Cari (search katalog).
3. Pilih buku (mis. MSIM420601).
4. Script mengambil daftar bab via list_modul.php (Pendahuluan, Modul 1-12, dst),
   lalu mengunduh tiap halaman gambar dan menggabungkannya menjadi 1 PDF.

KENAPA SEBELUMNYA AUTO-LOGOUT?
------------------------------
Endpoint list_modul.php / pemuat halaman dipanggil lewat AJAX di browser.
Jika dipanggil dari script tanpa header yang sesuai (X-Requested-With,
Referer yang benar, csrf_token, dan cookie sesi yang konsisten), server
menganggap request tidak sah lalu me-redirect ke logout -> muncul pesan
"Daftar bab gagal dimuat". Script ini memakai SATU requests.Session untuk
seluruh proses dan selalu mengirim header browser yang konsisten sehingga
sesi tetap hidup.

Catatan: nama field form login, parameter list_modul.php, dan pola URL gambar
bisa berbeda. Script ini berusaha mendeteksi otomatis (parsing form & link).
Jika ada yang tidak cocok, sesuaikan bagian yang ditandai dengan komentar # TODO.
"""

import os
import re
import sys
import time
import json
import html
import getpass
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

try:
    import img2pdf
    HAS_IMG2PDF = True
except ImportError:
    HAS_IMG2PDF = False

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
BASE = "https://pustaka.ut.ac.id/reader/"
URL_LOGIN = BASE + "index.php"
URL_DASHBOARD = BASE + "rbv.php"
URL_VIEW = BASE + "services/view.php"   # endpoint render gambar per halaman

# Format gambar halaman: 'jpg' (kualitas penuh) atau 'png'
PAGE_FORMAT = "jpg"

# Jeda antar-halaman (detik). Server pakai Sucuri WAF yang me-rate-limit
# request beruntun -> jangan terlalu cepat. Bisa diatur via env RBV_DELAY.
PAGE_DELAY = float(os.environ.get("RBV_DELAY", "0.8"))

# Backoff (detik) saat kena blokir 403 Sucuri, meningkat tiap percobaan.
BLOCK_BACKOFF = [10, 20, 40, 60, 90]

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

# Header browser konsisten -> kunci agar sesi tidak dianggap bot & tidak logout
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TIMEOUT = 30


# ---------------------------------------------------------------------------
# Util
# ---------------------------------------------------------------------------
def banner(msg):
    print("\n" + "=" * 60)
    print(msg)
    print("=" * 60)


def info(msg):
    print(f"ℹ️  {msg}")


def ok(msg):
    print(f"✅ {msg}")


def err(msg):
    print(f"❌ {msg}")


def slugify(text):
    text = re.sub(r"[^\w\-]+", "_", text.strip())
    return text.strip("_") or "untitled"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class RBVClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        self.csrf_token = None

    # ---- LOGIN ----------------------------------------------------------
    def fetch_login_page(self):
        r = self.s.get(URL_LOGIN, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    def login(self):
        banner("LOGIN RUANG BACA VIRTUAL UT")
        page = self.fetch_login_page()
        soup = BeautifulSoup(page, "html.parser")

        form = soup.find("form")
        if form is None:
            err("Form login tidak ditemukan. Mungkin sudah login / struktur berubah.")
            return self.is_logged_in()

        action = form.get("action") or URL_LOGIN
        post_url = urljoin(URL_LOGIN, action)

        # Kumpulkan semua field (termasuk hidden / csrf) supaya tidak ada yang hilang
        fields = {}
        for inp in form.find_all(["input", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            fields[name] = inp.get("value", "")

        # Simpan csrf token kalau ada (dipakai lagi untuk AJAX)
        for key in list(fields.keys()):
            if "csrf" in key.lower() or "token" in key.lower():
                self.csrf_token = fields[key]

        # Tampilkan soal captcha (math text) bila ada di halaman
        captcha_q = self._extract_captcha_question(soup)
        if captcha_q:
            print(f"\n🔐 Captcha: {captcha_q}")

        # Identifikasi field username / password / captcha secara fleksibel
        user_field = self._guess_field(fields, ["user", "nim", "email", "login", "username"])
        pass_field = self._guess_field(fields, ["pass", "password", "pwd"])
        cap_field = self._guess_field(fields, ["captcha", "code", "kode", "answer", "jawab", "hitung"])

        username = input("Username / NIM : ").strip()
        password = getpass.getpass("Password      : ")
        captcha = input("Jawaban captcha: ").strip() if cap_field or captcha_q else ""

        if user_field:
            fields[user_field] = username
        else:
            fields["username"] = username  # fallback # TODO sesuaikan nama field
        if pass_field:
            fields[pass_field] = password
        else:
            fields["password"] = password  # fallback # TODO sesuaikan nama field
        if cap_field:
            fields[cap_field] = captcha

        headers = {
            "Referer": URL_LOGIN,
            "Origin": BASE.rstrip("/"),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        r = self.s.post(post_url, data=fields, headers=headers,
                        timeout=TIMEOUT, allow_redirects=True)

        if self.is_logged_in(r.text):
            ok("Login berhasil. Sesi aktif.")
            self._refresh_csrf(r.text)
            return True
        err("Login gagal. Periksa username/password/captcha lalu coba lagi.")
        return False

    def _extract_captcha_question(self, soup):
        text = soup.get_text(" ", strip=True)
        m = re.search(r"(Berapa hasil dari\s*\d+\s*[\+\-\*xX]\s*\d+\s*=?)", text)
        if m:
            return m.group(1)
        m = re.search(r"(\d+\s*[\+\-\*xX]\s*\d+\s*=)", text)
        return m.group(1) if m else None

    @staticmethod
    def _guess_field(fields, keywords):
        for name in fields:
            low = name.lower()
            if any(k in low for k in keywords):
                return name
        return None

    def _refresh_csrf(self, html_text):
        m = re.search(r'csrfToken\s*=\s*"([0-9a-f]{16,})"', html_text)
        if not m:
            m = re.search(r'name=["\']csrf[^"\']*["\']\s+value=["\']([^"\']+)', html_text)
        if m:
            self.csrf_token = m.group(1)

    def is_logged_in(self, html_text=None):
        if html_text is None:
            r = self.s.get(URL_DASHBOARD, timeout=TIMEOUT)
            html_text = r.text
        # Dashboard punya tombol logout & teks khas
        return ("logout.php" in html_text) or ("Terakhir Dibaca" in html_text)

    # ---- DASHBOARD ------------------------------------------------------
    def fetch_dashboard(self):
        r = self.s.get(URL_DASHBOARD, timeout=TIMEOUT,
                       headers={"Referer": URL_LOGIN})
        r.raise_for_status()
        self._refresh_csrf(r.text)
        return r.text

    def parse_terakhir_dibaca(self, html_text):
        """Ambil daftar buku dari section 'Terakhir Dibaca'."""
        soup = BeautifulSoup(html_text, "html.parser")
        results = []
        # Cari section dengan judul 'Terakhir Dibaca'
        target_section = None
        for sec in soup.find_all(class_="section-island"):
            title = sec.find(class_="section-title")
            if title and "Terakhir Dibaca" in title.get_text():
                target_section = sec
                break
        scope = target_section or soup
        for a in scope.find_all("a", href=re.compile(r"index\.php\?modul=")):
            href = a.get("href")
            modul = parse_qs(urlparse(href).query).get("modul", [""])[0]
            title_el = a.find(class_="modul-title")
            judul = title_el.get_text(strip=True) if title_el else modul
            if modul and not any(r["modul"] == modul for r in results):
                results.append({"modul": modul, "judul": judul})
        return results

    def parse_catalog(self, html_text):
        """Ambil katalog lengkap dari variabel JS `catalog` di halaman."""
        m = re.search(r"const\s+catalog\s*=\s*(\[.*?\]);", html_text, re.S)
        if not m:
            return []
        raw = m.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # katalog mungkin terpotong -> ambil objek yang valid saja
            items = re.findall(r'\{"Modul":"([^"]+)","Judul":"([^"]+)"\}', raw)
            return [{"Modul": a, "Judul": b} for a, b in items]

    # ---- BUKU / BAB -----------------------------------------------------
    def open_book(self, modul):
        """GET halaman reader -> menetapkan Referer & konteks sesi yang benar.
        Ini WAJIB sebelum memuat dokumen agar sesi tidak ter-logout."""
        url = f"{URL_LOGIN}?modul={modul}"
        r = self.s.get(url, timeout=TIMEOUT, headers={"Referer": URL_DASHBOARD})
        r.raise_for_status()
        self._refresh_csrf(r.text)
        return url, r.text

    def get_chapters(self, modul, reader_html):
        """Ambil daftar dokumen/bab dari halaman reader.

        Tiap bab berupa link: index.php?subfolder=MODUL/&doc=XXX.pdf
        (mis. DAFIS=Daftar Isi, TINJAUAN=Tinjauan, M1..Mn=Modul 1..n).
        """
        soup = BeautifulSoup(reader_html, "html.parser")
        chapters = []
        seen = set()
        for a in soup.find_all("a", href=re.compile(r"subfolder=.*&doc=")):
            href = a["href"]
            qs = parse_qs(urlparse(href.replace("&amp;", "&")).query)
            doc_file = qs.get("doc", [""])[0]          # mis. "M1.pdf"
            sub = qs.get("subfolder", [f"{modul}/"])[0]
            if not doc_file or doc_file in seen:
                continue
            seen.add(doc_file)
            doc_name = re.sub(r"\.pdf$", "", doc_file, flags=re.I)  # "M1"
            judul = a.get_text(strip=True) or doc_name
            chapters.append({"doc": doc_name, "doc_file": doc_file,
                             "subfolder": sub, "judul": judul})
        if not chapters:
            try:
                with open("dbg_reader_fail.html", "w", encoding="utf-8") as f:
                    f.write(reader_html)
            except Exception:
                pass
            raise RuntimeError("Daftar bab gagal dimuat (tidak ada dokumen "
                               "ditemukan di halaman reader). "
                               "HTML disimpan ke dbg_reader_fail.html")
        return chapters

    # ---- HALAMAN GAMBAR -------------------------------------------------
    def get_doc_num_pages(self, chapter, reader_referer, retries=3):
        """Buka viewer dokumen untuk membaca jumlah halaman (numPages)."""
        url = f"{URL_LOGIN}?subfolder={chapter['subfolder']}&doc={chapter['doc_file']}"
        for attempt in range(retries):
            r = self.s.get(url, timeout=TIMEOUT, headers={"Referer": reader_referer})
            if r.status_code == 200:
                m = re.search(r"var\s+numPages\s*=\s*(\d+)", r.text)
                if m:
                    return int(m.group(1)), url
            # kemungkinan ter-throttle -> jeda lalu coba lagi
            time.sleep(1.5 * (attempt + 1))
        return 0, url

    def download_page(self, chapter, page, referer):
        """Unduh satu halaman sebagai gambar via services/view.php.

        Server dilindungi Sucuri WAF + proteksi anti-hotlink yang akan
        membalas 403 ("Akses gambar secara langsung dilarang") bila request
        terlalu cepat/banyak. Maka: pakai header gambar ala-browser, dan saat
        diblokir lakukan backoff panjang + 'sentuh' ulang halaman viewer.
        """
        params = {
            "doc": chapter["doc"],
            "format": PAGE_FORMAT,
            "subfolder": chapter["subfolder"],
            "page": str(page),
        }
        img_headers = {
            "Referer": referer,
            "Accept": ("image/avif,image/webp,image/apng,image/svg+xml,"
                       "image/*,*/*;q=0.8"),
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
        }
        for attempt in range(len(BLOCK_BACKOFF) + 1):
            try:
                r = self.s.get(URL_VIEW, params=params, timeout=TIMEOUT,
                               headers=img_headers)
            except requests.RequestException:
                time.sleep(5)
                continue
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and "image" in ctype and r.content:
                return r.content
            if os.environ.get("RBV_DEBUG"):
                try:
                    with open("dbg_view_fail.txt", "w", encoding="utf-8") as f:
                        f.write(f"URL: {r.url}\nstatus: {r.status_code}\n"
                                f"ctype: {ctype}\nbody: {r.text[:500]}")
                except Exception:
                    pass
            # Diblokir (kemungkinan 403 rate-limit). Backoff panjang lalu
            # refresh konteks viewer sebelum mencoba lagi.
            if attempt < len(BLOCK_BACKOFF):
                wait = BLOCK_BACKOFF[attempt]
                print(f"\n   ⏳ Terblokir sementara (status {r.status_code}). "
                      f"Menunggu {wait}s lalu coba lagi...", flush=True)
                time.sleep(wait)
                try:
                    self.s.get(referer, timeout=TIMEOUT,
                               headers={"Referer": URL_DASHBOARD})
                except requests.RequestException:
                    pass
        return None


# ---------------------------------------------------------------------------
# Proses unduh -> PDF
# ---------------------------------------------------------------------------
def build_pdf(image_paths, pdf_path):
    if not image_paths:
        err("Tidak ada gambar untuk dijadikan PDF.")
        return False
    if HAS_IMG2PDF:
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(image_paths))
    else:
        from PIL import Image
        imgs = [Image.open(p).convert("RGB") for p in image_paths]
        imgs[0].save(pdf_path, save_all=True, append_images=imgs[1:])
    return True


def download_book(client, modul, judul):
    banner(f"🚀 Memulai proses unduh untuk {modul} ({judul})")
    print(f"Membuka buku: {modul}...")

    reader_url, reader_html = client.open_book(modul)
    chapters = client.get_chapters(modul, reader_html)
    ok(f"Ditemukan {len(chapters)} bab/modul aktif.")
    for i, c in enumerate(chapters, 1):
        print(f"   {i:>2}. {c['judul']}  ({c['doc_file']})")

    book_dir = os.path.join(OUTPUT_DIR, slugify(modul))
    img_dir = os.path.join(book_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    ext = "." + ("jpg" if PAGE_FORMAT == "jpg" else PAGE_FORMAT)
    all_images = []
    for ci, chapter in enumerate(chapters, 1):
        num_pages, doc_url = client.get_doc_num_pages(chapter, reader_url)
        if num_pages <= 0:
            err(f"Bab {ci} ({chapter['judul']}): jumlah halaman tidak terbaca, dilewati.")
            continue
        info(f"Memuat bab {ci}/{len(chapters)}: {chapter['judul']} ({num_pages} halaman)")
        for page in range(1, num_pages + 1):
            fname = os.path.join(img_dir, f"{ci:03d}_{page:04d}{ext}")
            # Resume: lewati halaman yang sudah terunduh dengan benar.
            if os.path.exists(fname) and os.path.getsize(fname) > 1000:
                all_images.append(fname)
                print(f"\r   Bab {ci} - halaman {page}/{num_pages} (sudah ada)", end="")
                continue
            content = client.download_page(chapter, page, doc_url)
            if not content:
                err(f"   Gagal unduh halaman {page}")
                continue
            with open(fname, "wb") as f:
                f.write(content)
            all_images.append(fname)
            print(f"\r   Bab {ci} - halaman {page}/{num_pages} tersimpan", end="")
            time.sleep(PAGE_DELAY)  # sopan ke server (hindari rate-limit Sucuri)
        print()

    if not all_images:
        err("Tidak ada gambar terunduh.")
        return

    pdf_path = os.path.join(book_dir, f"{slugify(modul)}_{slugify(judul)}.pdf")
    if build_pdf(all_images, pdf_path):
        ok(f"PDF selesai: {pdf_path}  ({len(all_images)} halaman)")


# ---------------------------------------------------------------------------
# Menu interaktif
# ---------------------------------------------------------------------------
def choose_from_list(items, label):
    print()
    for i, it in enumerate(items, 1):
        print(f"  [{i}] {it['modul']}  -  {it['judul']}")
    while True:
        sel = input(f"\nPilih {label} (nomor / kode modul / 'q' batal): ").strip()
        if sel.lower() == "q":
            return None
        if sel.isdigit() and 1 <= int(sel) <= len(items):
            return items[int(sel) - 1]
        for it in items:
            if it["modul"].lower() == sel.lower():
                return it
        err("Pilihan tidak valid.")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    client = RBVClient()

    # 1) LOGIN (ulangi sampai berhasil / dibatalkan)
    while not client.login():
        again = input("Coba login lagi? (y/n): ").strip().lower()
        if again != "y":
            sys.exit(0)

    # 2) DASHBOARD
    dash = client.fetch_dashboard()

    while True:
        banner("MENU UTAMA")
        print("  [1] Terakhir Dibaca")
        print("  [2] Cari modul (search katalog)")
        print("  [q] Keluar")
        pilih = input("\nPilihan: ").strip().lower()

        if pilih == "q":
            break

        if pilih == "1":
            riwayat = client.parse_terakhir_dibaca(dash)
            if not riwayat:
                err("Tidak ada riwayat 'Terakhir Dibaca'.")
                continue
            book = choose_from_list(riwayat, "buku")
            if book:
                try:
                    download_book(client, book["modul"], book["judul"])
                except RuntimeError as e:
                    err(str(e))

        elif pilih == "2":
            catalog = client.parse_catalog(dash)
            if not catalog:
                err("Katalog tidak dapat dibaca dari halaman.")
                continue
            kw = input("Kata kunci (kode/judul): ").strip().lower()
            hasil = [{"modul": c["Modul"], "judul": c["Judul"]}
                     for c in catalog
                     if kw in c.get("Modul", "").lower()
                     or kw in c.get("Judul", "").lower()][:30]
            if not hasil:
                err("Tidak ada modul cocok.")
                continue
            book = choose_from_list(hasil, "buku")
            if book:
                try:
                    download_book(client, book["modul"], book["judul"])
                except RuntimeError as e:
                    err(str(e))
        else:
            err("Pilihan tidak dikenali.")

    print("\nSelesai. Sampai jumpa! 👋")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDibatalkan oleh pengguna.")
    except requests.RequestException as e:
        err(f"Masalah jaringan: {e}")
