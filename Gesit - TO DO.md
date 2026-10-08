# GESIT: Langkah Berikutnya (8 Okt sampai Demo Day 31 Okt)

Dibuat dari isi repo `KitaBisa-AI-Agentic-hackathon` (README, PRD.md, CLAUDE.md, folder `out/`) per 8 Okt 2026.
Rujukan ID seperti FR-SEL-2, POL-1, D-1 ada di `PRD.md`. Kode dan perintah ditulis apa adanya, penjelasan dalam bahasa Indonesia.

---

## 0. Kondisi sekarang

**Sudah ada**
- Dataset sintetis di `out/` (434 baris PO, 10 pemasok, 4 buyer, seed 42) dengan harga PIHPS asli (2 Sep 2024 sampai 30 Sep 2026, 543 hari).
- Generator (`generate.py`, `world.py`, `config.py`), validator, dan PRD lengkap dengan jadwal.
- Tiga skenario demo di `out/scenarios.json`.

**Masalah yang kelihatan di repo (urus dulu, Bagian 1)**
1. `README.md` masih ada penanda konflik merge (`<<<<<<< HEAD`, `=======`, `>>>>>>> origin/master`). Harus dibersihkan.
2. Skenario S2 hanya naik **+2,7%**, padahal aturan deteksi FR-DET-2 butuh lebih dari 3%. Demo S2 tidak akan terdeteksi.
3. Pemasok **100108** (yang harusnya "menurun") hampir tidak kelihatan: telat 11% sebelum Mar 2026, 15% sesudahnya. Grafik "sistem belajar" di demo akan datar.
4. Jenis pasar PIHPS (`price_type_id=3`) baru **dugaan** Pedagang Besar, belum dicocokkan dengan website.
5. Data kecil: 434 baris, hanya 15 baris holdout. AUC logistic regression 0,694 dan gradient boosting 0,603 pada 74 baris test itu angka yang berisik, jangan dijadikan klaim.
6. Efek Lebaran tidak muncul di data (17% telat di dalam jendela vs 24% di luar, n=18). Jangan klaim "sistem mengenali Lebaran".

**Catatan penting soal desain biaya.** Dengan biaya downtime Rp150 juta/hari, biaya stock-out (misal 3 hari = Rp450 juta) jauh lebih besar dari beda harga antar pemasok. Hasil "GESIT menang" akan sangat bergantung pada angka asumsi itu. Siapkan **analisis sensitivitas** (Bagian 5) supaya bisa dijawab kalau juri bertanya.

---

## 1. Hari ini sampai 9 Okt: rapikan data dan putuskan hal terbuka

### 1.1 Bersihkan README
Buka `README.md`, hapus tiga baris penanda konflik, dan buang baris `# KitaBisa-AI-Agentic-hackathon` yang dobel kalau tidak perlu. Lalu:
```bash
git add README.md && git commit -m "Fix README merge conflict markers"
```

### 1.2 Konfirmasi jenis pasar PIHPS
1. Buka https://www.bi.go.id/hargapangan, Tabel Harga, jenis **Pedagang Besar**, komoditas **Gula Pasir**, harian, nasional, 21 sampai 25 Sep 2026.
2. Bandingkan dengan `data/pihps/pihps_long.csv` untuk tanggal yang sama. PRD menyebut angkanya harus sekitar **Rp17.500 sampai Rp17.550** untuk Gula Pasir Lokal.
3. Kalau cocok, tulis "confirmed" di PRD bagian 17. Kalau tidak, jalankan probe dari README:
   ```bash
   python fetch_pihps.py --probe --start 2026-09-21 --end 2026-09-25
   ```
   Pilih id yang angkanya sama dengan website, lalu unduh ulang dengan `--price-type <id>`.

### 1.3 Perbaiki S2 (kenaikan harga)
Pilih satu (keputusan tim, catat di PRD bagian 17):
- **A (disarankan):** paksa S2 jadi kenaikan tetap, misalnya +6%. Sudah realistis untuk kejadian demo dan lolos aturan >3%.
- B: turunkan ambang FR-DET-2 jadi 2%. Lebih mudah, tapi ambang jadi terasa disetel supaya pas.

Cari bagian pembuat S2 di `generate.py` (cari `increase_pct` atau `price_increase`) dan ganti ukuran kenaikannya jadi nilai tetap di `config.py`. Jalankan ulang dan cek `out/scenarios.json`.

### 1.4 Perkuat penurunan pemasok 100108
Di `config.py`, naikkan efek penurunan 100108 (cari parameter drift atau tanggal Mar 2026 untuk pemasok itu). Targetnya: telat sekitar 10% sebelum perubahan, 35 sampai 40% sesudahnya. Setelah itu `python validate.py` dan lihat bagian 3 di `out/validation_report.md`.

### 1.5 (Opsional tapi disarankan) Tambah jumlah PO
Naikkan frekuensi pemesanan di `config.py` supaya total sekitar **800 sampai 1.000 baris**. Alasannya: SAP-RPT dan baseline butuh lebih banyak contoh, dan test set 74 baris terlalu kecil. Tetap cek AUC di `validate.py`: sehat kalau 0,65 sampai 0,85. Kalau di atas 0,9 tambah noise.

### 1.6 Generate ulang dan kunci
```bash
pip install -r requirements.txt
python generate.py
python validate.py
```
Di Windows pakai `py -3.11 generate.py`. Seed sama dan file PIHPS sama harus menghasilkan file yang identik. Commit hasilnya, lalu tulis tanggal dan parameter akhirnya di bagian "Dataset status" PRD.

### 1.7 Putuskan hal terbuka lain (PRD bagian 17)
- **Model Bedrock** untuk agen: putuskan **9 Okt**, lalu bekukan. Cek dulu di konsol AWS (region **us-west-2**) menu Bedrock, Model access, bahwa model pilihanmu sudah diaktifkan.
- **Ambang persetujuan:** Rp150 juta (PRD) vs Rp100 juta (rencana awal). Konfirmasi ke tim. Jadikan nilai konfigurasi, bukan angka di dalam kode.
- **Panel pemasok:** tambah kolom panel (mis. `LFA1-ZZPANEL`) dan satu penawaran dari pemasok **di luar panel** supaya POL-2 bisa didemokan.
- **Struktur folder:** pindahkan generator ke `data_gen/` sesuai PRD bagian 13. Lakukan satu commit khusus untuk pemindahan, jangan dicampur perubahan lain. Jangan lupa update path di `CLAUDE.md` dan README.

---

## 2. 5 sampai 11 Okt: inti agen di terminal (RPT di-stub dulu)

Targetnya: satu perintah di terminal yang membaca satu skenario, mendeteksi masalah, memeringkat pemasok pengganti, dan membuat draf PO. Belum perlu AWS atau SAP.

Aturan kerja dari `CLAUDE.md`: sebut ID requirement di rencana, komentar kode, dan pesan commit. Kode Python 3.11, type hints, fungsi kecil dan murni, tes `pytest` di folder `tests/`. LLM hanya menjelaskan angka, tidak menghitung.

### 2.1 `adapters/`: akses data berbentuk S/4
Satu modul yang membaca CSV di `out/` (kecuali `_truth/`) dan mengembalikan data dengan nama field SAP (LIFNR, EBELN, BEDAT, EINDT, NETPR, MENGE). Nanti diganti DynamoDB tanpa mengubah pemanggilnya (rule D-2).

### 2.2 `scoring/`: skor dan keandalan (FR-SEL-2, FR-EVA-2)
Murni Python, deterministik. Contoh kerangka (sudah dites jalan):

```python
from scipy.stats import beta

def expected_cost(qty, netpr, lead_days, p_late, d_late, cover_days, downtime_per_day):
    # semua waktu dalam hari dari hari ini
    on_time = max(0.0, lead_days - cover_days)
    late = max(0.0, lead_days + d_late - cover_days)
    stockout_days = p_late * late + (1 - p_late) * on_time
    return qty * netpr + stockout_days * downtime_per_day, stockout_days

def update_reliability(a, b, is_late, forget=0.97):
    a, b = a * forget, b * forget          # lupakan data lama pelan-pelan
    return (a, b + 1) if is_late else (a + 1, b)

def interval90(a, b):
    return beta.ppf(0.05, a, b), beta.ppf(0.95, a, b)
```
- Prior awal Beta(2, 1). `p_late` pengganti saat RPT mati = 1 dikurangi rata-rata posterior (FR-DET-4).
- Skor tampilan = 100 x (biaya terendah / biaya pemasok itu).
- Kalau `qty` lebih besar dari `AVAILABLE_KG_NEXT_30D`, tandai "tidak bisa penuh" dan hitung pesanan terbagi dengan pemasok peringkat berikutnya.
- Tulis tes: satu kasus tangan untuk `expected_cost`, satu untuk posterior (pemasok yang makin telat harus turun).

### 2.3 `tools/`: 14 tool di PRD bagian 8
Tulis sebagai fungsi Python biasa dulu (input dan output JSON), baru dibungkus Lambda nanti. Urutan pengerjaan: `get_open_pos`, `get_inventory`, `get_quotes`, `get_supplier_history`, `score_suppliers`, `predict_delay_risk` (stub: kembalikan `1 - posterior`, tandai `fallback=true`), `draft_po`, `submit_for_approval`, `release_po`, `record_override`, `record_outcome`, `get_integrity_flags`.

### 2.4 Deteksi (FR-DET)
Untuk tiap PO terbuka: tandai kalau P(late) >= 0,5, atau ada notice keterlambatan, kenaikan harga >3%, atau kekurangan kapasitas. Hitung tanggal stock-out = hari ini + `DAYS_OF_COVER` (sekarang 6,0 hari).

### 2.5 Agen pertama, tanpa AWS
Pakai Strands Agents secara lokal dengan satu supervisor yang memanggil sub-agen sebagai tool (**bukan Swarm**). Jalur eksekusi dibuat deterministik, temperature LLM 0,2 atau lebih rendah. Dulu bisa jalan dengan LLM apa pun yang kalian punya aksesnya, tapi untuk demo kunci ke model Bedrock yang diputuskan.

**Selesai bila:** `python -m agents.run S1` mencetak rencana, tabel peringkat pemasok, penjelasan di bawah 120 kata (FR-SEL-4), dan draf PO.

---

## 3. 12 sampai 18 Okt: SAP-RPT, Gen AI Hub, deploy ke AWS

### 3.1 Akses SAP (kerjakan lebih awal, jangan tunggu)
1. Pastikan **semua anggota** bisa membuka repo GitHub SAP dan baca panduan aktivasi begitu diunggah.
2. Pertanyaan atau kendala **hanya lewat tab Issues**. Jangan buat pull request.
3. Pertanyaan mentoring lewat Smartsheet **Senin 12, 19, 26 Okt sebelum 16.00 WIB**. Kirim pertanyaan konkret (error, kredensial, kuota), bukan pertanyaan umum.
4. Kalau akses AI Core (BAIP) terlambat: kembangkan pakai RPT Playground (API publik) di belakang interface yang sama. Fallback posterior dari 2.2 tetap jalan, jadi demo tidak mati.

### 3.2 `integrations/sap/`
- **Klien SAP-RPT:** kirim baris fitur dari `rpt_po_lines.csv` (kolom `SPLIT=train` sebagai konteks, hindari kolom `LATE` dan `DELAY_DAYS` pada baris yang diprediksi). Batas yang tertulis di rencana: sampai sekitar 128 baris prediksi per panggilan, **belum diverifikasi**, cek di panduan SAP.
- **Klien Gen AI Hub:** panggil lewat `sap-ai-sdk-gen` dengan masking nama buyer dan pemasok aktif. Kontrak PDF contoh (S3) dipakai untuk grounding klausul denda dan MOQ.
- **Kredensial:** jangan pernah di kode atau git. Lokal pakai `.env` (sudah di `.gitignore` bila ditambahkan, **cek**, saat ini `.gitignore` hanya berisi `test_out/`, `tests/FAKE_*`, `__pycache__/`; tambahkan `.env`). Di AWS pakai Secrets Manager.

### 3.3 Benchmark RPT (FR-EVA-5)
Di `eval/`, bandingkan SAP-RPT vs logistic regression vs gradient boosting pada **pemisahan waktu yang sama** (AUC dan Brier). Laporkan sebagai "pada data simulasi". Kalau RPT hanya menyamai baseline, katakan apa adanya dan tekankan nilai lain: prediksi sebelum gagal, keputusan berbasis biaya, guardrail.

### 3.4 AWS (region us-west-2 saja)
Urutan: (1) Lambda untuk tool, (2) AgentCore Gateway (MCP) yang menunjuk ke Lambda, (3) DynamoDB untuk tabel S/4 dan log keputusan, (4) deploy supervisor ke AgentCore Runtime, (5) Memory dan Observability, (6) Policy.
Ikuti dokumentasi AgentCore terbaru dan pakai Kiro dengan MCP AWS Docs dan Strands supaya perintahnya sesuai versi yang terpasang. Pasang **budget alert** dan hapus resource yang tidak terpakai setiap hari.

### 3.5 Policy (Cedar) dan tesnya
| ID | Aturan | Tes yang harus ada |
|---|---|---|
| POL-1 | Tolak `release_po` bila NETWR > Rp150 juta kecuali peran manager | 1 allow, 1 deny |
| POL-2 | Tolak `draft_po`/`release_po` untuk pemasok di luar panel | 1 allow, 1 deny |
| POL-3 | Tolak semua tool yang tidak terdaftar | 1 deny |
| POL-4 | Agen tidak boleh memanggil tool persetujuan atas nama sendiri | 1 deny |

---

## 4. 19 sampai 25 Okt: Integrity Guard, evaluasi, UI. **Feature freeze 25 Okt**

### 4.1 Integrity Guard (FR-INT)
- Override wajib alasan minimal 20 karakter, dicatat lengkap (waktu, buyer, rekomendasi, pilihan, selisih biaya, alasan).
- Pola mencurigakan: buyer b dengan pemasok s ditandai bila b punya **>= 5 PO** ke s, **share-nya >= 2x** share buyer lain, dan uji binomial satu sisi **p < 0,01**. Contoh (sudah dites jalan):

```python
from scipy.stats import binomtest

def pattern_flag(n_b_s, n_b, n_others_s, n_others):
    share_b, share_o = n_b_s / n_b, n_others_s / n_others
    p = binomtest(n_b_s, n_b, share_o, alternative="greater").pvalue
    return (n_b_s >= 5 and share_b >= 2 * share_o and p < 0.01), share_b, share_o, p
```
- Hasil yang diharapkan di data: **BUYER03 dengan 100107**. Sistem **menandai, bukan menuduh atau memblokir**. Tampilkan bukti (jumlah, share, override) di tampilan manager.

### 4.2 Monte Carlo (FR-EVA-4), di `eval/`
Hanya folder `eval/` yang boleh memakai `world.py` dan `_truth/` (rule D-1).
1. Untuk tiap skenario S1, S2, S3, jalankan **1.000 simulasi** untuk: pilihan GESIT, selalu termurah, selalu tercepat.
2. Catat rata-rata total biaya, rata-rata hari stock-out, dan P(ada stock-out).
3. **Analisis sensitivitas:** ulangi dengan downtime Rp50 juta, Rp150 juta, Rp300 juta per hari. Tunjukkan di mana GESIT menang dan di mana tidak. Ini jawaban jujur untuk pertanyaan "angkanya dari mana".
4. Beri label "simulated" di semua tabel dan slide (rule D-3).

### 4.3 Grafik keandalan (FR-EVA-3)
Garis posterior mean dan interval 90% per pemasok dari waktu ke waktu. Fokus 100108 untuk adegan 3e di demo.

### 4.4 UI Streamlit
- **Buyer view:** daftar PO berisiko, rekomendasi, tombol setuju atau override (dengan kotak alasan).
- **Manager view:** persetujuan PO di atas ambang dan daftar flag integritas.
- Semua teks UI dalam bahasa Inggris.

### 4.5 Kontrak PDF contoh
Buat 3 sampai 5 kontrak fiktif (denda keterlambatan, MOQ, masa berlaku) untuk pemasok yang dipakai di demo, simpan di S3, tandai "sample document". Bisa minta saya buatkan.

**Selesai bila:** S1, S2, S3 jalan dari UI, ada hasil Monte Carlo, ada grafik keandalan. Setelah 25 Okt hanya perbaikan bug dan penguatan demo.

---

## 5. 26 sampai 31 Okt: kuatkan demo dan latihan

1. **`scripts/reset_demo`:** mengembalikan database ke kondisi awal skenario. Seed tetap, temperature <= 0,2.
2. **10 kali berturut-turut** tiap skenario harus lolos sebelum 26 Okt (NFR-6). Catat yang gagal dan sebabnya.
3. **Waktu per keputusan:** target di bawah 60 detik (ideal 20). Ukur juga token dan panggilan RPT untuk slide biaya per keputusan (NFR-3).
4. **Video cadangan** dari satu run penuh. Kalau live gagal, langsung putar videonya.
5. **Deck** (Inggris): satu cerita (gula telat, lini berhenti), satu diagram arsitektur, satu slide asumsi (semua angka simulasi), hasil vs baseline, dampak.
6. **Latihan 3 kali dengan timer, dalam bahasa Inggris.** Total 15 menit termasuk Q&A, demo inti 6 menit. Siapkan jawaban untuk pertanyaan di PRD bagian 16, ditambah: "bagaimana kalau biaya downtime berbeda?" (jawab dengan hasil sensitivitas) dan "apakah data simulasi ini nyata?" (jawab: harga dari PIHPS, perilaku pemasok simulasi, semuanya berlabel).
7. Cek ulang tanggal Idul Fitri dan asumsi lain di `config.py` terhadap kalender resmi.

---

## 6. Pembagian kerja (saran, sesuaikan)

| Peran | Fokus |
|---|---|
| A: Data dan evaluasi | Bagian 1, 4.2, 4.3, benchmark RPT |
| B: Scoring dan agen | Bagian 2, supervisor Strands, penjelasan LLM |
| C: AWS dan SAP | Bagian 3: Lambda, Gateway, Policy, kredensial, akses SAP |
| D: UI, deck, demo | Bagian 4.4, 5, narasi dan latihan |

Satu orang jadi pemilik `PRD.md` dan Changelog. Setiap perubahan requirement ikut dicatat di Changelog pada commit yang sama.

---

## 7. Daftar cek mingguan

**Sampai 11 Okt**
- [ ] README bersih, PIHPS dikonfirmasi, S2 > 3%, drift 100108 kelihatan
- [ ] Model Bedrock diputuskan dan diaktifkan di us-west-2
- [ ] `python -m agents.run S1` jalan di terminal
- [ ] Semua anggota bisa masuk repo SAP

**Sampai 18 Okt**
- [ ] SAP-RPT (atau Playground) terpanggil dari `predict_delay_risk`
- [ ] Gen AI Hub memberi penjelasan dengan masking
- [ ] Gateway, Lambda, Policy hidup; tes allow/deny lulus
- [ ] Pertanyaan Smartsheet 12 Okt terkirim sebelum 16.00 WIB

**Sampai 25 Okt (freeze)**
- [ ] Integrity Guard menandai BUYER03 dengan 100107
- [ ] Monte Carlo dan sensitivitas selesai
- [ ] UI buyer dan manager jalan untuk S1, S2, S3

**Sampai 31 Okt**
- [ ] `reset_demo` dan 10 kali lolos
- [ ] Video cadangan dan deck selesai
- [ ] 3 kali latihan Inggris dengan timer

---

## 8. Hal yang belum terverifikasi

- Batas 128 baris per panggilan RPT dan performa RPT dengan ratusan baris (dari dokumen rencana, belum dicek).
- Apakah paket AI Core yang disediakan SAP cukup untuk RPT (tunggu panduan aktivasi).
- Jenis pasar PIHPS id 3 (Bagian 1.2).
- Perintah deploy AgentCore terbaru: ikuti dokumentasi resmi, jangan salin dari catatan lama.
