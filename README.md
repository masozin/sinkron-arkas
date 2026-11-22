# Aplikasi Sinkron ARKAS — Kelola BOS

Aplikasi kecil untuk menyinkronkan data ARKAS (kertas kerja, BKU,
referensi) ke fitur **Kelola BOS** di myschool. Tersedia dua cara
menjalankan: lewat jendela (UI) atau baris perintah (CLI). Keduanya
memakai mesin yang sama, `sinkron.py`.

## Isi folder

| Berkas | Isi |
| --- | --- |
| `sinkron.py` | Mesin sinkron (agen v4.2): membaca database ARKAS secara read-only dan mengirimkannya ke API myschool, per batch, dengan cek delta (tabel yang tidak berubah dilewati). |
| `sinkron_ui.py` | Jendela UI (CustomTkinter) untuk menjalankan mesin tanpa baris perintah. |
| `requirements.txt` | Daftar paket Python yang dibutuhkan. |
| `settings.json` | Dibuat otomatis oleh UI saat sinkron pertama dijalankan: path database + URL & token per tujuan. **Berisi token — jangan dibagikan, jangan di-commit** (sudah masuk `.gitignore`). |

## Syarat

- Windows dengan **Python 3.11** (perintah `py -3.11`). Paket
  `sqlcipher3` hanya menyediakan roda Windows untuk Python 3.10/3.11,
  jadi versi Python yang lebih baru tidak bisa dipakai.
- Pasang paket sekali saja:

  ```
  py -3.11 -m pip install -r requirements.txt
  ```

## Menjalankan lewat UI

```
py -3.11 sinkron_ui.py
```

1. **Database ARKAS** — otomatis terisi lokasi kerja ARKAS
   (`%APPDATA%\Arkas\arkas.db`) bila ada; tombol **Pilih…** untuk
   memilih berkas lain.
2. **Kata sandi database** — diketik setiap kali; tidak pernah
   disimpan di berkas mana pun.
3. **Tujuan** — *Hosting* (`https://myschool.ozinsq.my.id`) atau
   *Lokal* (`http://127.0.0.1:8000`). URL dan token tersimpan
   terpisah untuk tiap tujuan, jadi beralih tujuan tidak perlu
   mengetik ulang.
4. Centang **Sinkron penuh** hanya bila ingin mengirim ulang semua
   tabel (bawaan: sinkron delta — yang berubah saja).
5. Klik **Mulai Sinkron**; keluaran agen tampil baris demi baris di
   panel catatan.

Token sinkron dibuat di sisi myschool (perintah `php artisan
bos:token <id-sekolah>` di lokal, atau lewat SQL phpMyAdmin untuk
hosting — lihat panduan sinkron hosting Kelola BOS).

## Menjalankan lewat CLI

```
set ARKAS_PASSWORD=<kata sandi database ARKAS>
set BOS_TOKEN=<token sinkron>
py -3.11 sinkron.py --db C:\Users\SDN01_TTR\AppData\Roaming\Arkas\arkas.db --url https://myschool.ozinsq.my.id
```

Pilihan lain: `--penuh` (abaikan cek delta), `--tabel kas-umum,rapbs`
(batasi tabel). Variabel `ARKAS_DB`, `BOS_API_URL` juga bisa dipakai
sebagai bawaan `--db` / `--url`.

## Alur perbaikan (repo)

Repo ini berdiri sendiri supaya perbaikan berjalan seperti myschool:
perubahan dikirim sebagai patch, diterapkan dengan `git am`, lalu
digabung ke `main`. Berkas `settings.json`, database `*.db`, dan
`__pycache__` tidak ikut ter-commit.
