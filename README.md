# UT RBV Downloader

Script Python untuk mengunduh buku/modul dari **Ruang Baca Virtual (RBV) Universitas Terbuka** ([pustaka.ut.ac.id/reader](https://pustaka.ut.ac.id/reader/index.php)) dan menggabungkan seluruh babnya menjadi **satu file PDF**.

## Fitur

- 🔐 **Login otomatis** — username & password dimasukkan di terminal, captcha berbasis teks (mis. "Berapa hasil dari 6 + 8 =") diselesaikan otomatis/manual.
- 📚 **Dua mode**: pilih dari **Terakhir Dibaca** atau **Cari** modul di katalog.
- 📖 **Scrape seluruh bab** (Daftar Isi, Tinjauan, Modul 1–n) sampai mentok.
- 📄 **Gabung jadi 1 PDF** dengan kualitas gambar penuh (JPG).
- 🔁 **Resume** — halaman yang sudah terunduh otomatis dilewati bila dijalankan ulang.
- 🛡️ **Anti auto-logout & anti-blokir Sucuri** — memakai satu sesi konsisten, pacing antar-halaman, dan backoff bertingkat saat di-rate-limit.

## Instalasi

```bash
pip install -r requirements.txt
```

Dependensi: `requests`, `beautifulsoup4`, `img2pdf`, `Pillow`.

## Penggunaan

```bash
python3 ut_downloader.py
```

Alur:

1. Masukkan **username**, **password**, dan **jawaban captcha**.
2. Pilih menu **[1] Terakhir Dibaca** atau **[2] Cari modul**.
3. Pilih buku yang ingin diunduh.
4. Script mengunduh semua bab dan menyimpan hasilnya ke `downloads/<KODE_MODUL>/<KODE>_<Judul>.pdf`.

### Opsi (environment variable)

| Variabel | Default | Keterangan |
|----------|---------|------------|
| `RBV_DELAY` | `0.8` | Jeda (detik) antar-halaman. Perbesar (mis. `1.5`) bila sering kena blokir, perkecil (mis. `0.5`) bila ingin lebih cepat. |
| `RBV_DEBUG` | – | Set `1` untuk menyimpan respons gagal ke `dbg_view_fail.txt`. |

Contoh:

```bash
RBV_DELAY=1.5 python3 ut_downloader.py
```

## Cara kerja singkat

1. Login ke `index.php` (satu `requests.Session`, header browser konsisten).
2. Buka dashboard `rbv.php` untuk daftar riwayat & katalog.
3. Buka halaman reader `index.php?modul=KODE` → ambil daftar bab berupa link `index.php?subfolder=KODE/&doc=Mx.pdf`.
4. Untuk tiap bab, baca `numPages` dari viewer FlowPaper.
5. Unduh tiap halaman lewat `services/view.php?doc=Mx&format=jpg&subfolder=KODE/&page=N`.
6. Gabungkan semua gambar menjadi satu PDF (`img2pdf`).

## Catatan

- Server UT dilindungi **Sucuri WAF** yang membatasi request beruntun. Script sudah menangani ini dengan jeda + backoff otomatis, jadi proses bisa berjalan lama namun tetap tuntas.
- Sesekali server bisa mengembalikan halaman kosong (status 200 tapi tanpa data). Halaman seperti ini otomatis dilewati setelah beberapa kali percobaan.
- Hanya untuk penggunaan pribadi atas materi yang Anda miliki aksesnya. Patuhi hak cipta dan ketentuan layanan UT.

## Lisensi

Penggunaan pribadi.