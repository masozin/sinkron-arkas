"""Antarmuka (UI) agen sinkron ARKAS -> myschool Kelola BOS.

Jendela sederhana untuk menjalankan sinkron.py tanpa baris
perintah: pilih database ARKAS, isi kata sandi + token, pilih
tujuan (Hosting / Lokal), klik Mulai Sinkron, dan lihat catatan
keluaran agen mengalir baris demi baris.

Yang disimpan (settings.json di folder ini): path database,
tujuan terakhir, dan URL + token per tujuan.
KATA SANDI DATABASE TIDAK PERNAH DISIMPAN — selalu diketik ulang.

Jalankan dengan Python 3.11 di Windows:
    py -3.11 sinkron_ui.py
"""

import json
import os
import subprocess
import sys
import threading

import customtkinter as ctk

URL_AWAL = {
    "hosting": "https://myschool.ozinsq.my.id",
    "lokal": "http://127.0.0.1:8000",
}

NAMA_FILE_PENGATURAN = "settings.json"


# ── Pengaturan ────────────────────────────────────────────────

def _path_pengaturan():
    """Lokasi settings.json: selalu di folder berkas ini."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        NAMA_FILE_PENGATURAN)


def _pengaturan_awal():
    return {
        "db_path": "",
        "tujuan": "hosting",
        "tujuan_cfg": {
            nama: {"url": url, "token": ""}
            for nama, url in URL_AWAL.items()
        },
    }


def muat_pengaturan():
    """Baca settings.json; gabung di atas nilai awal supaya kunci
    baru di masa depan tidak membuat berkas lama error."""
    hasil = _pengaturan_awal()
    try:
        with open(_path_pengaturan(), "r", encoding="utf-8") as f:
            tersimpan = json.load(f)
    except (OSError, ValueError):
        return hasil
    if not isinstance(tersimpan, dict):
        return hasil
    hasil["db_path"] = str(tersimpan.get("db_path", "") or "")
    if tersimpan.get("tujuan") in URL_AWAL:
        hasil["tujuan"] = tersimpan["tujuan"]
    cfg = tersimpan.get("tujuan_cfg")
    if isinstance(cfg, dict):
        for nama in URL_AWAL:
            bagian = cfg.get(nama)
            if isinstance(bagian, dict):
                hasil["tujuan_cfg"][nama] = {
                    "url": str(bagian.get("url") or URL_AWAL[nama]),
                    "token": str(bagian.get("token") or ""),
                }
    return hasil


def simpan_pengaturan(pengaturan):
    """Tulis settings.json (tanpa kata sandi — memang tidak ada
    kunci kata sandi di struktur pengaturan)."""
    data = {
        "db_path": pengaturan.get("db_path", ""),
        "tujuan": pengaturan.get("tujuan", "hosting"),
        "tujuan_cfg": pengaturan.get("tujuan_cfg", {}),
    }
    with open(_path_pengaturan(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Perintah & proses ─────────────────────────────────────────

def bangun_perintah(cfg):
    """Susun (argv, env) untuk menjalankan sinkron.py.

    cfg: dict dengan kunci db_path, url, token, sandi, penuh.
    Kata sandi & token diteruskan LEWAT ENV, tidak lewat argv
    (argv bisa terlihat di daftar proses).
    """
    folder = os.path.dirname(os.path.abspath(__file__))
    argv = [sys.executable, os.path.join(folder, "sinkron.py"),
            "--db", cfg["db_path"], "--url", cfg["url"]]
    if cfg.get("penuh"):
        argv.append("--penuh")
    env = dict(os.environ)
    env["ARKAS_PASSWORD"] = cfg.get("sandi", "")
    env["BOS_TOKEN"] = cfg.get("token", "")
    env["PYTHONUNBUFFERED"] = "1"
    return argv, env


def jalankan_sinkron(cfg, on_line, on_selesai):
    """Jalankan sinkron.py di thread terpisah, keluaran dialirkan
    per baris ke on_line(str); di akhir on_selesai(kode_keluar)
    dipanggil sekali. Mengembalikan thread-nya."""
    def _kerja():
        argv, env = bangun_perintah(cfg)
        try:
            proc = subprocess.Popen(
                argv, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except OSError as e:
            on_line(f"!! Gagal memulai proses sinkron: {e}")
            on_selesai(1)
            return
        for baris in proc.stdout:
            on_line(baris.rstrip("\n"))
        on_selesai(proc.wait())

    thread = threading.Thread(target=_kerja, daemon=True)
    thread.start()
    return thread


# ── Jendela ───────────────────────────────────────────────────

def _db_bawaan():
    """Path database ARKAS bawaan: %APPDATA%\\Arkas\\arkas.db
    bila berkasnya ada (lokasi kerja aplikasi ARKAS)."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        kandidat = os.path.join(appdata, "Arkas", "arkas.db")
        if os.path.isfile(kandidat):
            return kandidat
    return ""


class AplikasiSinkron(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Sinkron ARKAS — Kelola BOS")
        self.geometry("720x560")
        self.minsize(640, 480)

        self.pengaturan = muat_pengaturan()
        self.tujuan_var = ctk.StringVar(
            value=self.pengaturan.get("tujuan", "hosting"))
        self._sedang_jalan = False

        self._bangun()
        # Isi awal dari pengaturan tersimpan / bawaan.
        db = self.pengaturan.get("db_path") or _db_bawaan()
        self.db_var.set(db)
        self._tampil_tujuan(self.tujuan_var.get())

    # -- tata letak --
    def _bangun(self):
        pad = {"padx": 12, "pady": 4}

        ctk.CTkLabel(self, text="Database ARKAS").grid(
            row=0, column=0, sticky="w", **pad)
        self.db_var = ctk.StringVar()
        ctk.CTkEntry(self, textvariable=self.db_var).grid(
            row=0, column=1, sticky="ew", **pad)
        ctk.CTkButton(self, text="Pilih…", width=80,
                      command=self._pilih_db).grid(
            row=0, column=2, **pad)

        ctk.CTkLabel(self, text="Kata sandi database").grid(
            row=1, column=0, sticky="w", **pad)
        self.sandi_var = ctk.StringVar()
        ctk.CTkEntry(self, textvariable=self.sandi_var,
                     show="•").grid(row=1, column=1, sticky="ew", **pad)
        ctk.CTkLabel(self, text="(tidak disimpan)").grid(
            row=1, column=2, sticky="w", **pad)

        ctk.CTkLabel(self, text="Tujuan").grid(
            row=2, column=0, sticky="w", **pad)
        bingkai = ctk.CTkFrame(self, fg_color="transparent")
        bingkai.grid(row=2, column=1, sticky="w", **pad)
        ctk.CTkRadioButton(
            bingkai, text="Hosting", variable=self.tujuan_var,
            value="hosting",
            command=lambda: self._tampil_tujuan("hosting"),
        ).pack(side="left", padx=(0, 16))
        ctk.CTkRadioButton(
            bingkai, text="Lokal", variable=self.tujuan_var,
            value="lokal",
            command=lambda: self._tampil_tujuan("lokal"),
        ).pack(side="left")

        ctk.CTkLabel(self, text="URL myschool").grid(
            row=3, column=0, sticky="w", **pad)
        self.url_var = ctk.StringVar()
        ctk.CTkEntry(self, textvariable=self.url_var).grid(
            row=3, column=1, sticky="ew", **pad)

        ctk.CTkLabel(self, text="Token sinkron").grid(
            row=4, column=0, sticky="w", **pad)
        self.token_var = ctk.StringVar()
        ctk.CTkEntry(self, textvariable=self.token_var,
                     show="•").grid(row=4, column=1, sticky="ew", **pad)

        self.penuh_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self, text="Sinkron penuh (abaikan cek delta)",
            variable=self.penuh_var,
        ).grid(row=5, column=1, sticky="w", **pad)

        self.tombol = ctk.CTkButton(
            self, text="Mulai Sinkron", command=self._mulai)
        self.tombol.grid(row=6, column=1, sticky="w", **pad)

        self.status_var = ctk.StringVar(value="Siap.")
        ctk.CTkLabel(self, textvariable=self.status_var).grid(
            row=7, column=0, columnspan=3, sticky="w", **pad)

        self.catatan = ctk.CTkTextbox(self, state="disabled")
        self.catatan.grid(row=8, column=0, columnspan=3,
                          sticky="nsew", padx=12, pady=(4, 12))

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(8, weight=1)

    # -- interaksi --
    def _pilih_db(self):
        from tkinter import filedialog
        terpilih = filedialog.askopenfilename(
            title="Pilih database ARKAS",
            filetypes=[("Database ARKAS", "*.db"), ("Semua berkas", "*.*")],
        )
        if terpilih:
            self.db_var.set(terpilih)

    def _tampil_tujuan(self, nama):
        cfg = self.pengaturan["tujuan_cfg"][nama]
        self.url_var.set(cfg["url"])
        self.token_var.set(cfg["token"])

    def _simpan_isian(self):
        """Salin isian sekarang ke struktur pengaturan (per tujuan
        terpisah) dan simpan ke settings.json."""
        nama = self.tujuan_var.get()
        self.pengaturan["tujuan_cfg"][nama] = {
            "url": self.url_var.get().strip(),
            "token": self.token_var.get().strip(),
        }
        self.pengaturan["db_path"] = self.db_var.get().strip()
        self.pengaturan["tujuan"] = nama
        simpan_pengaturan(self.pengaturan)

    def _tulis(self, teks):
        self.catatan.configure(state="normal")
        self.catatan.insert("end", teks + "\n")
        self.catatan.see("end")
        self.catatan.configure(state="disabled")

    def _mulai(self):
        if self._sedang_jalan:
            return
        db = self.db_var.get().strip()
        sandi = self.sandi_var.get()
        token = self.token_var.get().strip()
        url = self.url_var.get().strip()
        if not db or not os.path.isfile(db):
            self._tulis("!! Database ARKAS belum dipilih atau berkasnya "
                        "tidak ditemukan — sinkron dibatalkan.")
            return
        if not sandi:
            self._tulis("!! Kata sandi database masih kosong — "
                        "sinkron dibatalkan.")
            return
        if not token:
            self._tulis("!! Token sinkron masih kosong — "
                        "sinkron dibatalkan.")
            return
        if not url:
            self._tulis("!! URL myschool masih kosong — "
                        "sinkron dibatalkan.")
            return

        self._simpan_isian()
        cfg = {
            "db_path": db, "url": url, "token": token,
            "sandi": sandi, "penuh": bool(self.penuh_var.get()),
        }
        self._sedang_jalan = True
        self.tombol.configure(state="disabled")
        self.status_var.set("Sedang sinkron…")
        self._tulis(f"— Memulai sinkron ke {url} —")
        jalankan_sinkron(
            cfg,
            on_line=lambda baris: self.after(0, self._tulis, baris),
            on_selesai=lambda kode: self.after(0, self._selesai, kode),
        )

    def _selesai(self, kode):
        self._sedang_jalan = False
        self.tombol.configure(state="normal")
        if kode == 0:
            self.status_var.set("Sinkron selesai.")
        else:
            self.status_var.set(
                "Selesai dengan kegagalan — lihat catatan terakhir.")


def main():
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    AplikasiSinkron().mainloop()


if __name__ == "__main__":
    main()
