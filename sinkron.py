"""Agen sinkron Kelola BOS — ARKAS -> myschool (v4.2).

Membaca database ARKAS (SQLite SQLCipher) secara READ-ONLY dan
mengirim cermin tabel-tabel yang dibutuhkan fitur Kelola BOS ke
API myschool:

    POST {BOS_API_URL}/api/bos/sinkron/{tabel}
    Authorization: Bearer {BOS_TOKEN}

Konfigurasi lewat environment variable / argumen (tidak ada
yang tertanam di kode):
    ARKAS_DB        path file arkas.db        (--db)
    ARKAS_PASSWORD  password database ARKAS   (--password)
    BOS_API_URL     base URL myschool         (--url, default http://127.0.0.1:8000)
    BOS_TOKEN       token sinkron sekolah     (--token)

Jalankan dengan Python yang memiliki sqlcipher3 (lihat venv
di folder ini). Contoh:

    ARKAS_PASSWORD=... BOS_TOKEN=... \\
        ./venv/bin/python sinkron.py --db arkas.db

Sinkron DELTA (v4.2, Fase 1.9): sebelum mengirim, agen meminta
sidik jari server (GET /api/bos/sinkron/cek: jumlah baris +
arkas_last_update maksimum per tabel). Tabel yang sidik
jarinya sama persis dengan keadaan lokal DILEWATI; tabel yang
berbeda hanya dikirimi baris yang berubah sejak sidik jari
server (dengan tumpang-tindih 2 detik), lalu diverifikasi
ulang — bila jumlah akhir tetap tidak sama, tabel itu dikirim
PENUH (menyembuhkan diri). Server lama tanpa endpoint cek
menjawab 404: agen mundur anggun ke sinkron penuh persis
seperti v4.1. Pilihan tambahan:
    --penuh         abaikan cek, kirim semua tabel utuh
    --tabel a,b,c   batasi ke tabel-tabel itu saja

Prinsip:
- Database ARKAS tidak pernah diubah (mode=ro).
- Semua baris dikirim termasuk yang soft_delete, supaya cermin
  di server sama persis; halaman myschool yang menyaring.
- Upsert di server berdasarkan (sekolah_id, id_arkas), jadi
  sinkron berulang aman (idempoten).

Perubahan v3 (Fase 1.5):
- Tabel baru: ref-acuan-barang (semua tahun, ±60 ribu baris)
  dan ref-satuan.
- ref-kode & ref-rekening kini dikirim UTUH (semua baris
  tahun 2024-2026), tidak lagi hanya yang terpakai. Kunci
  ref_kode di ARKAS komposit (id_ref_kode, tahun,
  sumber_dana_id, bentuk_pendidikan_id) sehingga id_arkas
  cerminnya pun komposit.
- anggaran membawa is_revisi + create_date (arkas_create_date)
  untuk pengurutan & pelabelan versi kertas kerja.

Perubahan v4 (Fase 1.6):
- Tabel baru: rapbs-periode (jadwal bulanan/triwulan per
  baris kertas kerja, semua baris termasuk soft_delete) dan
  ref-periode (nama periode). Halaman Kertas Kerja memakai
  rapbs-periode untuk kolom Jadwal dan menghubungkan
  realisasi BKU lewat kas_umum.id_rapbs_periode.

Perubahan v4.1:
- Putus koneksi di tengah batch ditangkap per batch (dulu
  membunuh seluruh proses): coba ulang 3x dengan jeda, lalu
  batch dibelah dua rekursif sampai 25 baris; ringkasan
  kegagalan per baris, tanpa traceback.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from sqlcipher3 import dbapi2 as sqlcipher

VERSI_AGEN = "4.2.0"
UKURAN_BATCH = 500
BATAS_BELAH = 25  # batch gagal jaringan dibelah dua sampai ukuran ini

TABEL_SEMUA = [
    "ref-bku", "ref-periode", "ref-satuan", "ref-kode",
    "ref-rekening", "ref-acuan-barang", "anggaran", "penjab",
    "kas-nota", "kas-umum", "rapbs", "rapbs-periode",
]


def kirim(base_url, token, tabel, baris):
    """POST satu batch ke API; kembalikan dict jawaban server.

    Kalau server menjawab error (mis. HTTP 500), isi jawaban
    server ikut dicetak — di myschool mode debug isinya pesan
    exception Laravel, jadi penyebabnya langsung terlihat dan
    tidak perlu menebak dari traceback saja.
    """
    url = base_url.rstrip("/") + "/api/bos/sinkron/" + tabel
    body = json.dumps({"agen_versi": VERSI_AGEN, "baris": baris}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            isi = e.read().decode("utf-8", "replace")
        except Exception:
            isi = "(tidak bisa membaca isi jawaban server)"
        print(f"  !! {tabel}: server menjawab HTTP {e.code}")
        print(f"  !! isi jawaban server: {isi[:2000]}")
        raise


def _kirim_dengan_ulangan(base_url, token, tabel, baris):
    """Kirim satu batch; kembalikan (tersimpan, baris_gagal).

    Kegagalan JARINGAN (koneksi diputus server di tengah jalan,
    timeout, DNS, dsb.) dicoba ulang sampai 3 kali dengan jeda.
    Kalau tetap gagal dan batch masih besar, batch DIBELAH DUA
    dan tiap paruh dicoba sendiri — mengisolasi segmen bermasalah
    (mis. server hosting mematikan proses pada satu kiriman)
    tanpa mengorbankan sisa tabel. Kegagalan HTTP (server menjawab
    dengan kode error) tidak diulang: isi jawaban server sudah
    dicetak oleh kirim() dan batch dihitung gagal apa adanya.
    """
    import http.client
    import time

    galat_jaringan = (urllib.error.URLError, ConnectionError,
                      http.client.HTTPException, TimeoutError, OSError)
    percobaan = 0
    while True:
        try:
            hasil = kirim(base_url, token, tabel, baris)
            return hasil.get("tersimpan", 0), 0
        except urllib.error.HTTPError:
            return 0, len(baris)
        except galat_jaringan as e:
            percobaan += 1
            if percobaan >= 3:
                break
            jeda = 3 if percobaan == 1 else 8
            print(f"  !! {tabel}: koneksi terputus ({type(e).__name__}); "
                  f"mencoba lagi dalam {jeda} detik (percobaan {percobaan + 1}/3) ...")
            time.sleep(jeda)
    if len(baris) > BATAS_BELAH:
        tengah = len(baris) // 2
        print(f"  !! {tabel}: batch {len(baris)} baris tetap gagal; dibelah dua dan dicoba per paruh ...")
        simpan1, gagal1 = _kirim_dengan_ulangan(base_url, token, tabel, baris[:tengah])
        simpan2, gagal2 = _kirim_dengan_ulangan(base_url, token, tabel, baris[tengah:])
        return simpan1 + simpan2, gagal1 + gagal2
    print(f"  !! {tabel}: {len(baris)} baris menyerah setelah dicoba ulang (koneksi tetap terputus).")
    return 0, len(baris)


def kirim_bertahap(base_url, token, tabel, semua_baris):
    """Kirim satu tabel per batch; kembalikan (tersimpan, gagal).

    Satu batch yang gagal tidak menghentikan tabel lain:
    tabel berikutnya tetap dikirim, kegagalan dilaporkan di
    ringkasan akhir supaya bisa diperbaiki dan diulang.
    """
    total = 0
    gagal = 0
    jumlah_batch = (len(semua_baris) + UKURAN_BATCH - 1) // UKURAN_BATCH
    for nomor, i in enumerate(range(0, len(semua_baris), UKURAN_BATCH), start=1):
        tersimpan, baris_gagal = _kirim_dengan_ulangan(
            base_url, token, tabel, semua_baris[i:i + UKURAN_BATCH])
        total += tersimpan
        gagal += baris_gagal
        if jumlah_batch >= 10 and (nomor % 10 == 0 or nomor == jumlah_batch):
            print(f"  {tabel}: batch {nomor}/{jumlah_batch} ...")
    if gagal:
        print(f"  {tabel}: {len(semua_baris)} baris terkirim, {total} tersimpan, {gagal} baris GAGAL (lihat pesan di atas)")
    else:
        print(f"  {tabel}: {len(semua_baris)} baris terkirim, {total} tersimpan")
    return total, gagal


def ambil_cek(base_url, token):
    """GET sidik jari server; kembalikan dict slug -> {jumlah, maks_update}.

    None bila server tidak mendukung (404/405), token ditolak,
    koneksi gagal, atau jawaban bukan JSON yang diharapkan —
    pemanggil mundur ke sinkron penuh.
    """
    url = base_url.rstrip("/") + "/api/bos/sinkron/cek"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            hasil = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return hasil if isinstance(hasil, dict) else None


def potong19(nilai):
    """Samakan format waktu ke detik: 'YYYY-MM-DD HH:MM:SS' (19 char)."""
    if not nilai:
        return None
    return str(nilai)[:19]


def sidik_jari_lokal(data):
    """(jumlah baris, arkas_last_update maksimum terpotong detik)."""
    maks = None
    for baris in data:
        nilai = baris.get("arkas_last_update")
        if nilai:
            nilai = str(nilai)[:19]
            if maks is None or nilai > maks:
                maks = nilai
    return len(data), maks


def kirim_tabel(base_url, token, tabel, data, cek_server, paksa_penuh):
    """Kirim satu tabel (penuh / dilewati / delta); kembalikan baris gagal.

    cek_server: hasil ambil_cek() atau None (server lama).
    Delta: hanya baris dengan arkas_last_update melewati
    (maks server - 2 detik); sesudahnya jumlah diverifikasi
    ulang lewat cek — tidak sama berarti kirim penuh tabel ini.
    """
    if paksa_penuh or cek_server is None:
        _, gagal = kirim_bertahap(base_url, token, tabel, data)
        return gagal

    sidik = cek_server.get(tabel)
    if not isinstance(sidik, dict) or "jumlah" not in sidik:
        _, gagal = kirim_bertahap(base_url, token, tabel, data)
        return gagal

    jumlah_lokal, maks_lokal = sidik_jari_lokal(data)
    jumlah_server = int(sidik.get("jumlah") or 0)
    maks_server = potong19(sidik.get("maks_update"))

    if jumlah_lokal == jumlah_server and maks_lokal == maks_server:
        print(f"  {tabel}: tidak berubah, dilewati")
        return 0

    if maks_server is None:
        # Cermin server kosong untuk tabel ini: kirim utuh.
        _, gagal = kirim_bertahap(base_url, token, tabel, data)
        return gagal

    try:
        ambang_dt = datetime.strptime(maks_server, "%Y-%m-%d %H:%M:%S") - timedelta(seconds=2)
    except ValueError:
        _, gagal = kirim_bertahap(base_url, token, tabel, data)
        return gagal
    ambang = ambang_dt.strftime("%Y-%m-%d %H:%M:%S") + ".000"
    delta = [r for r in data if (r.get("arkas_last_update") or "") > ambang]
    print(f"  {tabel}: delta — {len(delta)} baris berubah dari {jumlah_lokal}")
    if delta:
        _, gagal = kirim_bertahap(base_url, token, tabel, delta)
        if gagal:
            return gagal

    # Verifikasi pasca-delta: jumlah akhir harus sama persis.
    cek_ulang = ambil_cek(base_url, token)
    if cek_ulang is not None:
        sidik_ulang = cek_ulang.get(tabel) or {}
        if int(sidik_ulang.get("jumlah") or 0) != jumlah_lokal:
            print(f"  {tabel}: jumlah server masih berbeda setelah delta — mengirim penuh tabel ini")
            _, gagal = kirim_bertahap(base_url, token, tabel, data)
            return gagal
    return 0


def baris_dict(cur, sql, params=()):
    cur.execute(sql, params)
    kolom = [d[0] for d in cur.description]
    return [dict(zip(kolom, r)) for r in cur.fetchall()]


def b(nilai):
    """Nilai ARKAS 0/1/None -> bool/None untuk JSON."""
    if nilai is None:
        return None
    return bool(int(nilai))


def main():
    ap = argparse.ArgumentParser(description="Sinkron cermin ARKAS ke Kelola BOS myschool")
    ap.add_argument("--db", default=os.environ.get("ARKAS_DB", "arkas.db"))
    ap.add_argument("--password", default=os.environ.get("ARKAS_PASSWORD", ""))
    ap.add_argument("--url", default=os.environ.get("BOS_API_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--token", default=os.environ.get("BOS_TOKEN", ""))
    ap.add_argument("--penuh", action="store_true",
                    help="abaikan cek sidik jari; kirim semua tabel utuh")
    ap.add_argument("--tabel", default="",
                    help="batasi ke tabel tertentu, pisah koma (mis. kas-umum,rapbs)")
    args = ap.parse_args()

    dipilih = None
    if args.tabel:
        dipilih = [t.strip() for t in args.tabel.split(",") if t.strip()]
        asing = [t for t in dipilih if t not in TABEL_SEMUA]
        if asing:
            print("Tabel tidak dikenal: " + ", ".join(asing), file=sys.stderr)
            print("Tabel yang sah: " + ", ".join(TABEL_SEMUA), file=sys.stderr)
            return 1

    if not args.password:
        print("ARKAS_PASSWORD belum diisi.", file=sys.stderr)
        return 1
    if not args.token:
        print("BOS_TOKEN belum diisi (buat dengan: php artisan bos:token <sekolah_id>).", file=sys.stderr)
        return 1

    conn = sqlcipher.connect(f"file:{args.db}?mode=ro", uri=True)
    # PRAGMA key tidak menerima parameter terikat di sqlcipher3;
    # password berasal dari env lokal, kutip tunggal diloloskan.
    conn.execute("PRAGMA key = '%s'" % args.password.replace("'", "''"))
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    cur = conn.cursor()

    nama_sumber_dana = {
        r["id_ref_sumber_dana"]: r["nama_sumber_dana"]
        for r in baris_dict(cur, "SELECT id_ref_sumber_dana, nama_sumber_dana FROM ref_sumber_dana")
    }

    # ── anggaran ──────────────────────────────────────────
    anggaran = []
    for r in baris_dict(cur, "SELECT * FROM anggaran"):
        anggaran.append({
            "id_arkas": r["id_anggaran"],
            "id_ref_sumber_dana": int(r["id_ref_sumber_dana"]),
            "nama_sumber_dana": nama_sumber_dana.get(int(r["id_ref_sumber_dana"])),
            "tahun_anggaran": int(r["tahun_anggaran"]),
            "volume": r["volume"],
            "harga_satuan": r["harga_satuan"],
            "jumlah": r["jumlah"],
            "sisa_anggaran": r["sisa_anggaran"],
            "is_pengesahan": b(r["is_pengesahan"]),
            "is_approve": int(r["is_approve"]),
            "is_revisi": int(r["is_revisi"]),
            "arkas_create_date": r["create_date"],
            "is_aktif": b(r["is_aktif"]),
            "tanggal_pengajuan": r["tanggal_pengajuan"],
            "tanggal_pengesahan": r["tanggal_pengesahan"],
            "soft_delete": b(r["soft_delete"]),
            "arkas_last_update": r["last_update"],
        })

    # ── ref_bku ───────────────────────────────────────────
    ref_bku = [{
        "id_arkas": str(r["id_ref_bku"]),
        "id_ref_bku": int(r["id_ref_bku"]),
        "nama_bku": r["bku"],
        "kode_bku": r["kode_bku"],
        "arkas_last_update": r["last_update"],
    } for r in baris_dict(cur, "SELECT * FROM ref_bku")]

    # ── ref_satuan ────────────────────────────────────────
    ref_satuan = [{
        "id_arkas": r["ref_satuan_id"],
        "satuan": r["satuan"],
        "unit": r["unit"],
    } for r in baris_dict(cur, "SELECT * FROM ref_satuan")]

    # ── ref_kode: SEMUA baris (kunci ARKAS komposit) ──────
    ref_kode = []
    for r in baris_dict(cur, "SELECT * FROM ref_kode ORDER BY tahun, id_kode"):
        kunci = "{}#{}#{}#{}".format(
            r["id_ref_kode"],
            int(r["tahun"]) if r["tahun"] is not None else "",
            int(r["sumber_dana_id"]) if r["sumber_dana_id"] is not None else "",
            int(r["bentuk_pendidikan_id"]) if r["bentuk_pendidikan_id"] is not None else "",
        )
        ref_kode.append({
            "id_arkas": kunci,
            "id_ref_kode": r["id_ref_kode"],
            "id_kode": r["id_kode"],
            "uraian_kode": r["uraian_kode"],
            "id_level_kode": int(r["id_level_kode"]) if r["id_level_kode"] is not None else None,
            "sumber_dana_id": int(r["sumber_dana_id"]) if r["sumber_dana_id"] is not None else None,
            "bentuk_pendidikan_id": int(r["bentuk_pendidikan_id"]) if r["bentuk_pendidikan_id"] is not None else None,
            "tahun": int(r["tahun"]) if r["tahun"] is not None else None,
            "arkas_last_update": r["last_update"],
        })

    # ── ref_rekening: SEMUA baris ─────────────────────────
    ref_rekening = []
    for r in baris_dict(cur, "SELECT * FROM ref_rekening"):
        ref_rekening.append({
            "id_arkas": f"{r['kode_rekening']}|{int(r['tahun'])}",
            "kode_rekening": r["kode_rekening"],
            "nama_rekening": r["rekening"],
            "tahun": int(r["tahun"]) if r["tahun"] is not None else None,
            "arkas_last_update": r["last_update"],
        })

    # ── ref_acuan_barang: SEMUA baris, semua tahun ────────
    ref_acuan_barang = []
    for r in baris_dict(cur, "SELECT * FROM ref_acuan_barang"):
        ref_acuan_barang.append({
            "id_arkas": f"{r['id_barang']}#{int(r['tahun'])}",
            "id_barang": r["id_barang"],
            "kode_rekening": r["kode_rekening"],
            "nama_barang": r["nama_barang"],
            "satuan": r["satuan"],
            "blok_id": r["blok_id"],
            "kode_belanja": r["kode_belanja"],
            "harga_barang": r["harga_barang"],
            "batas_bawah": r["batas_bawah"],
            "batas_atas": r["batas_atas"],
            "tahun": int(r["tahun"]),
            "arkas_last_update": r["last_update"],
        })

    # ── penjab ────────────────────────────────────────────
    penjab = [{
        "id_arkas": f"{r['id_penjab']}|{int(r['tahun'])}",
        "id_penjab_arkas": r["id_penjab"],
        "tahun": int(r["tahun"]),
        "tanggal_mulai": r["tanggal_mulai"],
        "tanggal_selesai": r["tanggal_selesai"],
        "ks": r["ks"],
        "nip_ks": r["nip_ks"],
        "bendahara": r["bendahara"],
        "nip_bendahara": r["nip_bendahara"],
        "komite": r["komite"],
        "soft_delete": b(r["soft_delete"]),
        "arkas_last_update": r["last_update"],
    } for r in baris_dict(cur, "SELECT * FROM sekolah_penjab")]

    # ── kas_umum_nota ─────────────────────────────────────
    nota = [{
        "id_arkas": r["id_kas_nota"],
        "no_bukti": r["no_bukti"],
        "tanggal_nota": r["tanggal_nota"],
        "no_nota": r["no_nota"],
        "nama_toko": r["nama_toko"],
        "alamat_toko": r["alamat_toko"],
        "no_telp": r["no_telp"],
        "is_badan_usaha": b(r["is_badan_usaha"]),
        "npwp": r["npwp"],
        "total": r["total"],
        "kode_rekening": r["kode_rekening"],
        "id_ref_kode": r["id_ref_kode"],
        "has_ppn": b(r["has_ppn"]),
        "has_pph_22": b(r["has_pph_22"]),
        "alasan_pecah_nota": r["alasan_pecah_nota"],
        "is_beli_di_siplah": b(r["is_beli_di_siplah"]),
        "details": r["details"],
        "soft_delete": b(r["soft_delete"]),
        "arkas_last_update": r["last_update"],
    } for r in baris_dict(cur, "SELECT * FROM kas_umum_nota")]

    # ── kas_umum ──────────────────────────────────────────
    kas = [{
        "id_arkas": r["id_kas_umum"],
        "id_kas_nota": r["id_kas_nota"],
        "id_rapbs_periode": r["id_rapbs_periode"],
        "id_anggaran_arkas": r["id_anggaran"],
        "kode_rekening": r["kode_rekening"],
        "id_ref_bku": int(r["id_ref_bku"]),
        "tanggal_transaksi": r["tanggal_transaksi"],
        "no_bukti": r["no_bukti"],
        "uraian": r["uraian"],
        "uraian_pajak": r["uraian_pajak"],
        "saldo": r["saldo"],
        "status_bku": r["status_bku"],
        "volume": r["volume"],
        "is_ppn": b(r["is_ppn"]),
        "is_pph_21": b(r["is_pph_21"]),
        "is_pph_22": b(r["is_pph_22"]),
        "is_pph_23": b(r["is_pph_23"]),
        "is_pph_4": b(r["is_pph_4"]),
        "is_sspd": b(r["is_sspd"]),
        "is_lock": b(r["is_lock"]),
        "soft_delete": b(r["soft_delete"]),
        "arkas_last_update": r["last_update"],
    } for r in baris_dict(cur, "SELECT * FROM kas_umum")]

    # ── rapbs ─────────────────────────────────────────────
    rapbs = [{
        "id_arkas": r["id_rapbs"],
        "id_anggaran_arkas": r["id_anggaran"],
        "id_ref_kode": r["id_ref_kode"],
        "id_ref_tahun_anggaran": int(r["id_ref_tahun_anggaran"]) if r["id_ref_tahun_anggaran"] is not None else None,
        "kode_rekening": r["kode_rekening"],
        "id_barang": r["id_barang"],
        "urutan": r["urutan"],
        "uraian": r["uraian"],
        "uraian_text": r["uraian_text"],
        "volume": r["volume"],
        "satuan": r["satuan"],
        "harga_satuan": r["harga_satuan"],
        "jumlah": r["jumlah"],
        "v1": r["v1"], "s1": r["s1"],
        "v2": r["v2"], "s2": r["s2"],
        "v3": r["v3"], "s3": r["s3"],
        "v4": r["v4"], "s4": r["s4"],
        "keterangan": r["keterangan"],
        "soft_delete": b(r["soft_delete"]),
        "arkas_last_update": r["last_update"],
    } for r in baris_dict(cur, "SELECT * FROM rapbs")]

    # ── ref_periode ───────────────────────────────────────
    ref_periode = [{
        "id_arkas": str(int(r["id_periode"])),
        "id_periode": int(r["id_periode"]),
        "periode": r["periode"],
        "arkas_last_update": r["last_update"],
    } for r in baris_dict(cur, "SELECT * FROM ref_periode")]

    # ── rapbs_periode (jadwal per baris kertas kerja) ─────
    rapbs_periode = [{
        "id_arkas": r["id_rapbs_periode"],
        "id_rapbs_arkas": r["id_rapbs"],
        "id_periode": int(r["id_periode"]),
        "volume": r["volume"],
        "satuan": r["satuan"],
        "harga_satuan": r["harga_satuan"],
        "jumlah": r["jumlah"],
        "soft_delete": b(r["soft_delete"]),
        "arkas_last_update": r["last_update"],
    } for r in baris_dict(cur, "SELECT * FROM rapbs_periode")]

    conn.close()

    print(f"Mengirim ke {args.url} ...")
    # Sidik jari server untuk sinkron delta (kecuali --penuh).
    # Server lama tanpa endpoint cek -> mundur ke sinkron penuh.
    cek_server = None
    if not args.penuh:
        cek_server = ambil_cek(args.url, args.token)
        if cek_server is None:
            print("  (server belum mendukung cek cepat — sinkron penuh seperti biasa)")
    # Urutan: referensi & anggaran dulu, baru transaksi.
    # Satu tabel yang gagal tidak menghentikan tabel lain;
    # di akhir dilaporkan tabel mana yang perlu diulang.
    gagal_tabel = []
    for tabel, data in [
        ("ref-bku", ref_bku),
        ("ref-periode", ref_periode),
        ("ref-satuan", ref_satuan),
        ("ref-kode", ref_kode),
        ("ref-rekening", ref_rekening),
        ("ref-acuan-barang", ref_acuan_barang),
        ("anggaran", anggaran),
        ("penjab", penjab),
        ("kas-nota", nota),
        ("kas-umum", kas),
        ("rapbs", rapbs),
        ("rapbs-periode", rapbs_periode),
    ]:
        if dipilih is not None and tabel not in dipilih:
            continue
        gagal = kirim_tabel(args.url, args.token, tabel, data, cek_server, args.penuh)
        if gagal:
            gagal_tabel.append(tabel)
    if gagal_tabel:
        print("Sinkron SELESAI DENGAN KEGAGALAN pada tabel: " + ", ".join(gagal_tabel))
        print("Perbaiki penyebabnya (lihat isi jawaban server di atas), lalu jalankan lagi — baris yang sudah tersimpan tidak akan ganda.")
        return 1
    print("Sinkron selesai.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
