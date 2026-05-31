import streamlit as st
import pandas as pd
import plotly.express as px
import pulp
from datetime import datetime
import copy

# =====================================================================
# KONFIGURASI HALAMAN
# =====================================================================
st.set_page_config(
    page_title="Penjadwalan Kamar Operasi — RS Polri",
    layout="wide",
    page_icon="🏥",
    initial_sidebar_state="expanded",
)

# =====================================================================
# 1. KONSTANTA & DAFTAR PILIHAN
# =====================================================================
DAFTAR_DOKTER = [
    "dr. Fidelis Heru, Sp.OT", "dr. Ivan, Sp.OT",
    "dr. Reza Abidin, Sp.OT", "dr. Sashia, Sp.OT",
    "dr. Sigit Wedhanto, Sp.OT", "dr. Sofwan, Sp.OT",
    "dr. Sumpada, Sp.OT", "dr. Wira, Sp.OT",
    "dr. Yogi A, Sp.OT (K) Spine", "dr. Zarkasyi, Sp.OT",
]

DAFTAR_WARNA = [
    '#FF595E', '#FFCA3A', '#8AC926', '#1982C4', '#6A4C93',
    '#F15BB5', '#00BBF9', '#9B5DE5', '#FB5607', '#FF006E',
    '#8338EC', '#3A86FF', '#2EC4B6', '#E76F51', '#457B9D',
]
PETA_WARNA = {f"OP {i}": DAFTAR_WARNA[(i - 1) % len(DAFTAR_WARNA)] for i in range(1, 100)}
for _r in ['OK 1', 'OK 2', 'OK 3']:
    PETA_WARNA[f"Istirahat ({_r})"] = "#cbd5e1"  # abu-abu untuk blok buffer

# Pilihan ramah-pengguna -> nilai teknis (skala) untuk skor ASA
PETA_ASA = {
    "1 — Sehat (tanpa penyakit penyerta)": 1,
    "2 — Penyakit ringan terkontrol": 2,
    "3 — Penyakit berat terkontrol": 3,
    "4 — Penyakit berat mengancam jiwa": 4,
    "5 — Kritis, harapan hidup rendah tanpa operasi": 5,
    "6 — Mati batang otak (donor organ)": 6,
}
PETA_KLAS = {
    "Grade 1 — Ringan": 1,
    "Grade 2 — Sedang": 2,
    "Grade 3 — Cukup berat": 3,
    "Grade 4 — Berat": 4,
    "Grade 5 — Sangat berat": 5,
}


def _default_config():
    cfg = {
        "RUANGAN": ['OK 1', 'OK 2', 'OK 3'],
        "RUANGAN_TANPA_CARM": ['OK 3'],
        "BOBOT_RUANGAN": {'OK 1': 0, 'OK 2': 0, 'OK 3': 0},
        "JAM_START": 7, "JAM_END": 23,

        # --- SISTEM BUFFER BARU (Session-Based) ---
        "JAM_ISTIRAHAT_START": 12,   # buffer boleh muncul mulai jam ini
        "JAM_ISTIRAHAT_MAX": 14,     # batas akhir buffer boleh muncul
        "DURASI_ISTIRAHAT": 1,       # lama blok istirahat/transisi (jam)

        # --- PENALTI ---
        "PENALTI_GESER_JAM": 100,    # per jam pergeseran dari jadwal terkunci
        "PENALTI_PINDAH_OK": 50,     # bila pindah ruangan dari jadwal terkunci
        "PENALTI_PAKAI_OK3": 500,    # OK 3 sebagai ruangan cadangan

        # --- BOBOT SKOR KEPENTINGAN KLINIS (E_i) ---
        "BOBOT_URGENCY": 1000,
        "BOBOT_ASA": 100,
        "BOBOT_KLASIFIKASI": 50,
        "BOBOT_IMPLAN": 25,
    }
    cfg["BOBOT_JAM"] = {t: (24 - t) for t in range(cfg["JAM_START"], cfg["JAM_END"] + 1)}
    cfg["SLOT_WAKTU"] = list(range(cfg["JAM_START"], cfg["JAM_END"]))
    return cfg


for _key, _val in {'CONFIG': _default_config(), 'DATABASE_RS': {}}.items():
    if _key not in st.session_state:
        st.session_state[_key] = _val


def init_sesi(tgl_str):
    if tgl_str not in st.session_state.DATABASE_RS:
        st.session_state.DATABASE_RS[tgl_str] = {}
    s = st.session_state.DATABASE_RS[tgl_str]
    s.setdefault("semua_pasien", [])
    s.setdefault("df_jadwal", None)
    s.setdefault("df_buffer", None)
    s.setdefault("memori_terkunci", {})
    s.setdefault("gagal", [])
    s.setdefault("log_edit", [])
    s.setdefault("log_batal", [])
    s.setdefault("trace_log", [])
    s.setdefault("z_score", None)
    s.setdefault("total_reward", None)
    s.setdefault("total_penalti", None)
    s.setdefault("status_solver", None)
    return s


# =====================================================================
# 2. SKOR KEPENTINGAN KLINIS (E_i)
# =====================================================================
def hitung_ei(p, config):
    """E_i = (emergency × W_urg) + (ASA × W_asa) + (klasifikasi × W_klas) + (implan × W_imp)."""
    is_emg = 1 if p.get('emergency', 0) == 1 else 0
    asa = int(p.get('asa', 1))
    klas = int(p.get('klasifikasi', 1))
    imp = int(p.get('implan', 0))
    ei = (is_emg * config["BOBOT_URGENCY"]
          + asa * config["BOBOT_ASA"]
          + klas * config["BOBOT_KLASIFIKASI"]
          + imp * config["BOBOT_IMPLAN"])
    return max(ei, 1)


# =====================================================================
# 3. SESSION-BASED BUFFER (SISTEM BARU)
# =====================================================================
def terapkan_session_buffer(data_mentah, config):
    """
    Sistem baru:
    - Pasien EMERGENCY memakai durasi maksimum (d_max) sebagai pengaman.
    - Pasien ELEKTIF memakai durasi rata-rata (d_avg).
    - Ditambahkan satu blok BUFFER 'mengambang' (floating) per ruangan,
      yang akan ditempatkan otomatis di antara jam istirahat
      (JAM_ISTIRAHAT_START s/d JAM_ISTIRAHAT_MAX) sebagai transisi sesi.
    """
    data_robust = []
    for p in data_mentah:
        pc = copy.deepcopy(p)
        is_emg = 1 if pc.get('emergency', 0) == 1 else 0
        if is_emg:
            pc['durasi'] = pc['d_max']
            pc['status_robust'] = "Darurat — durasi maksimum"
        else:
            pc['durasi'] = pc['d_avg']
            pc['status_robust'] = "Elektif — durasi rata-rata"
        pc['is_buffer'] = False
        data_robust.append(pc)

    id_dummy = 900
    for r in config["RUANGAN"]:
        id_dummy += 1
        data_robust.append({
            "id": id_dummy,
            "tindakan": "Istirahat / Transisi Sesi",
            "pref_start": config["JAM_ISTIRAHAT_START"],
            "durasi": config["DURASI_ISTIRAHAT"],
            "d_min": config["DURASI_ISTIRAHAT"],
            "d_avg": config["DURASI_ISTIRAHAT"],
            "d_max": config["DURASI_ISTIRAHAT"],
            "c_arm": 0, "dokter": "-", "asa": 0, "klasifikasi": 0, "implan": 0,
            "emergency": 0, "is_buffer": True, "target_ruang": r,
            "status_robust": "Blok istirahat",
        })
    return sorted(data_robust, key=lambda x: x['id'])


# =====================================================================
# 4. MESIN OPTIMASI RMILP (PuLP / CBC)
# =====================================================================
def rmilp_pulp(data_pasien, config, memori_terkunci, jam_sekarang):
    RGN = config["RUANGAN"]
    RGN_TANPA_CARM = config.get("RUANGAN_TANPA_CARM", [])
    END = config["JAM_END"]
    WKT = config["SLOT_WAKTU"]
    BBT = config["BOBOT_JAM"]
    BBT_RGN = config["BOBOT_RUANGAN"]
    mem = memori_terkunci or {}

    patients = [p['id'] for p in data_pasien]
    model = pulp.LpProblem("OR_Session_Scheduling", pulp.LpMaximize)
    S = pulp.LpVariable.dicts("S", (patients, RGN, WKT), cat='Binary')
    X = pulp.LpVariable.dicts("X", (patients, RGN, WKT), cat='Binary')

    biaya = {}
    obj_terms = []

    # ---------- FUNGSI OBJEKTIF ----------
    for p_data in data_pasien:
        pid = p_data['id']
        if p_data.get('is_buffer'):
            continue  # buffer tidak punya reward/penalti

        E_i = hitung_ei(p_data, config)
        t_old = mem[pid]['waktu_mulai'] if (pid in mem and mem[pid]) else None
        r_old = mem[pid]['ruangan'] if (pid in mem and mem[pid]) else None

        biaya[pid] = {}
        for r in RGN:
            biaya[pid][r] = {}
            for t in WKT:
                rew = (BBT.get(t, 0) + BBT_RGN.get(r, 0)) * E_i
                pen = 0
                # Penalti pakai OK 3 (ruangan cadangan)
                if r == 'OK 3':
                    pen += config["PENALTI_PAKAI_OK3"]
                # Penalti reschedule terhadap jadwal terkunci
                if t_old is not None:
                    pen += abs(t - t_old) * config["PENALTI_GESER_JAM"]
                if r_old is not None and r != r_old:
                    pen += config["PENALTI_PINDAH_OK"]
                net = rew - pen
                biaya[pid][r][t] = {"net": net, "rew": rew, "pen": pen, "ei": E_i}
                obj_terms.append(S[pid][r][t] * net)

    model += pulp.lpSum(obj_terms)

    # ---------- KENDALA ----------
    for p_data in data_pasien:
        pid = p_data['id']
        dur = int(p_data['durasi'])
        pref = int(p_data['pref_start'])
        c_arm = int(p_data.get('c_arm', 0))

        if p_data.get('is_buffer'):
            # Floating buffer: tepat 1 slot di ruangan target, dalam jendela istirahat
            r_target = p_data['target_ruang']
            model += pulp.lpSum([S[pid][r_target][t] for t in WKT]) == 1
            for r in RGN:
                for t in WKT:
                    if r != r_target:
                        model += S[pid][r][t] == 0
                    elif t < config["JAM_ISTIRAHAT_START"] or t > config["JAM_ISTIRAHAT_MAX"]:
                        model += S[pid][r][t] == 0
        else:
            # Kendala 1: Single Start
            model += pulp.lpSum([S[pid][r][t] for r in RGN for t in WKT]) <= 1
            for r in RGN:
                # Kendala 2: C-Arm
                if c_arm == 1 and r in RGN_TANPA_CARM:
                    for t in WKT:
                        model += X[pid][r][t] == 0
                        model += S[pid][r][t] == 0
                for t in WKT:
                    # Kendala 3: Rolling Horizon (kunci masa lalu)
                    if t < jam_sekarang:
                        if pid in mem and mem[pid] and t == mem[pid].get('waktu_mulai') and r == mem[pid].get('ruangan'):
                            model += S[pid][r][t] == 1
                        else:
                            model += S[pid][r][t] == 0
                    # Kendala 4: Release Date
                    if t < pref:
                        model += S[pid][r][t] == 0

        # Kendala 5: Duration Match (berlaku untuk semua, termasuk buffer)
        ind = pulp.lpSum([S[pid][r][t] for r in RGN for t in WKT])
        model += pulp.lpSum([X[pid][r][t] for r in RGN for t in WKT]) == dur * ind

        for r in RGN:
            for t in WKT:
                # Kendala 6: Non-preemptive + Kendala 7: Deadline
                if t + dur <= END:
                    for k in range(t, t + dur):
                        if k in WKT:
                            model += X[pid][r][k] >= S[pid][r][t]
                else:
                    model += S[pid][r][t] == 0

    for t in WKT:
        # Kendala 8: Kapasitas Ruangan
        for r in RGN:
            model += pulp.lpSum([X[pid][r][t] for pid in patients]) <= 1
        # Kendala 9: Kapasitas Pasien
        for pid in patients:
            model += pulp.lpSum([X[pid][r][t] for r in RGN]) <= 1

    # Kendala 10: Ketersediaan Dokter (1 dokter tidak di 2 kamar bersamaan)
    dokter_map = {}
    for p_data in data_pasien:
        if p_data.get('is_buffer'):
            continue
        doc = str(p_data.get('dokter', 'Anonim')).strip()
        dokter_map.setdefault(doc, []).append(p_data['id'])

    for doc, list_pid in dokter_map.items():
        if len(list_pid) > 1:
            for t in WKT:
                model += pulp.lpSum([X[pid][r][t] for pid in list_pid for r in RGN]) <= 1

    model.solve(pulp.PULP_CBC_CMD(msg=0))
    status = pulp.LpStatus[model.status]

    hasil, hasil_buffer, gagal, trace = [], [], [], []
    Z_tot = pulp.value(model.objective) or 0
    R_tot, P_tot = 0.0, 0.0

    for p_data in data_pasien:
        pid = p_data['id']
        terjadwal = False
        for r in RGN:
            for t in WKT:
                val = pulp.value(S[pid][r][t])
                if val is not None and round(val) == 1:
                    terjadwal = True
                    if p_data.get('is_buffer'):
                        hasil_buffer.append({
                            'id': pid, 'ruangan': r, 'jam_mulai': t,
                            'jam_selesai': t + p_data['durasi'],
                            'durasi': p_data['durasi'],
                            'tindakan': p_data['tindakan'],
                        })
                        continue
                    b = biaya[pid][r][t]
                    R_tot += b["rew"]
                    P_tot += b["pen"]
                    hasil.append({
                        'id': pid, 'ruangan': r, 'jam_mulai': t,
                        'jam_selesai': t + p_data['durasi'],
                        'durasi': p_data['durasi'],
                        'status_robust': p_data.get('status_robust', 'Elektif'),
                        'tindakan': p_data['tindakan'],
                        'dokter': p_data['dokter'],
                        'c_arm': int(p_data['c_arm']),
                        'emergency': int(p_data.get('emergency', 0)),
                        'pref_start': p_data['pref_start'],
                        'geser': (t != p_data['pref_start']),
                        'cost': b["net"], 'ei': b["ei"],
                        'asa': p_data.get('asa', 1),
                        'klasifikasi': p_data.get('klasifikasi', 1),
                        'implan': p_data.get('implan', 0),
                    })
                    te = {
                        'Operasi': f"OP {pid}",
                        'Tindakan': p_data['tindakan'],
                        'Ruangan': r,
                        'Jam Dijadwalkan': f"{t:02d}:00",
                        'Jam Target': f"{p_data['pref_start']:02d}:00",
                        'Skor Klinis (E_i)': b["ei"],
                        'Nilai Positif': round(b["rew"], 1),
                        'Pengurang (Penalti)': round(b["pen"], 1),
                        'Skor Bersih': round(b["net"], 1),
                        'Geser (jam)': t - p_data['pref_start'],
                    }
                    det = []
                    if r == 'OK 3':
                        det.append(f"pakai OK 3 (+{config['PENALTI_PAKAI_OK3']})")
                    if pid in mem and mem[pid]:
                        mp = mem[pid]
                        dlt = abs(t - mp.get('waktu_mulai', t))
                        if dlt > 0:
                            det.append(f"geser {dlt} jam (+{dlt * config['PENALTI_GESER_JAM']})")
                        if r != mp.get('ruangan'):
                            det.append(f"pindah ruangan (+{config['PENALTI_PINDAH_OK']})")
                    te['Catatan'] = " + ".join(det) if det else "—"
                    trace.append(te)
        if not terjadwal and not p_data.get('is_buffer'):
            gagal.append(pid)

    return (hasil, hasil_buffer, gagal, trace,
            round(Z_tot, 1), round(R_tot, 1), round(P_tot, 1), status)


# =====================================================================
# 5. HELPER DATAFRAME & MEMORI
# =====================================================================
def hasil_ke_dataframe(hasil_jadwal, tanggal_str):
    tgl_obj = datetime.strptime(tanggal_str, "%Y-%m-%d")
    rows = []
    for item in hasil_jadwal:
        rows.append({
            "Tanggal": tanggal_str,
            "Operasi": f"OP {item['id']}",
            "id_num": item['id'],
            "Prioritas": "🚨 Darurat" if item['emergency'] == 1 else "Terjadwal",
            "Tindakan": item['tindakan'],
            "Ruangan": item['ruangan'],
            "Waktu": f"{item['jam_mulai']:02d}:00 - {item['jam_selesai']:02d}:00",
            "Durasi (jam)": item['durasi'],
            "Catatan Durasi": item['status_robust'],
            "Dokter": item['dokter'],
            "C-Arm": "Ya" if item['c_arm'] == 1 else "Tidak",
            "Jam Target": f"{item['pref_start']:02d}:00",
            "Bergeser?": "Ya" if item['geser'] else "—",
            "Skor Klinis": item['ei'],
            "ASA": item['asa'],
            "Klasifikasi": item['klasifikasi'],
            "Implan": "Ya" if item['implan'] == 1 else "Tidak",
            "Skor Bersih": item['cost'],
            "Start_DT": tgl_obj.replace(hour=item['jam_mulai']),
            "Finish_DT": tgl_obj.replace(hour=item['jam_selesai']),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def buffer_ke_dataframe(hasil_buffer, tanggal_str):
    tgl_obj = datetime.strptime(tanggal_str, "%Y-%m-%d")
    rows = []
    for item in hasil_buffer:
        rows.append({
            "Operasi": f"Istirahat ({item['ruangan']})",
            "id_num": item['id'],
            "Prioritas": "Istirahat",
            "Tindakan": item['tindakan'],
            "Ruangan": item['ruangan'],
            "Waktu": f"{item['jam_mulai']:02d}:00 - {item['jam_selesai']:02d}:00",
            "Durasi (jam)": item['durasi'],
            "Dokter": "-",
            "Skor Klinis": "-",
            "Start_DT": tgl_obj.replace(hour=item['jam_mulai']),
            "Finish_DT": tgl_obj.replace(hour=item['jam_selesai']),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def update_memori(df_jadwal):
    mem = {}
    if df_jadwal is None or df_jadwal.empty:
        return mem
    for _, row in df_jadwal.iterrows():
        pid = row['id_num']
        jm = int(row['Waktu'].split(":")[0])
        mem[pid] = {
            'ruangan': row['Ruangan'],
            'waktu_mulai': jm,
            'durasi': int(row['Durasi (jam)']),
        }
    return mem


def run_optimasi(sesi, cfg, jam_skrg, tgl_str):
    # Pengaman: pastikan jendela & durasi istirahat selalu muat sebelum jam tutup,
    # supaya blok buffer tidak membuat seluruh jadwal mustahil disusun.
    if cfg["JAM_ISTIRAHAT_MAX"] < cfg["JAM_ISTIRAHAT_START"]:
        cfg["JAM_ISTIRAHAT_MAX"] = cfg["JAM_ISTIRAHAT_START"]
    if cfg["JAM_ISTIRAHAT_MAX"] + cfg["DURASI_ISTIRAHAT"] > cfg["JAM_END"]:
        cfg["JAM_ISTIRAHAT_MAX"] = cfg["JAM_END"] - cfg["DURASI_ISTIRAHAT"]
    if cfg["JAM_ISTIRAHAT_START"] + cfg["DURASI_ISTIRAHAT"] > cfg["JAM_END"]:
        cfg["JAM_ISTIRAHAT_START"] = cfg["JAM_END"] - cfg["DURASI_ISTIRAHAT"]

    data_r = terapkan_session_buffer(sesi["semua_pasien"], cfg)
    (hasil, hasil_buf, gagal, trace, Z, R, P, status) = rmilp_pulp(
        data_r, cfg, sesi["memori_terkunci"], jam_skrg
    )
    df_baru = hasil_ke_dataframe(hasil, tgl_str)
    df_buf = buffer_ke_dataframe(hasil_buf, tgl_str)
    sesi.update({
        "df_jadwal": df_baru,
        "df_buffer": df_buf,
        "memori_terkunci": update_memori(df_baru),
        "gagal": gagal,
        "trace_log": trace,
        "z_score": Z,
        "total_reward": R,
        "total_penalti": P,
        "status_solver": status,
    })
    return df_baru, gagal, Z, status


# =====================================================================
# 6. GANTT CHART
# =====================================================================
def buat_gantt(df, df_buffer, tanggal_str, cfg, key="gantt"):
    if (df is None or df.empty) and (df_buffer is None or df_buffer.empty):
        st.info("Belum ada jadwal untuk ditampilkan.")
        return

    tgl_obj = datetime.strptime(tanggal_str, "%Y-%m-%d")

    frames = []
    if df is not None and not df.empty:
        frames.append(df[["Operasi", "Ruangan", "Start_DT", "Finish_DT",
                           "Prioritas", "Dokter", "Tindakan", "Waktu",
                           "Durasi (jam)", "Jam Target", "Bergeser?",
                           "Skor Klinis"]].copy())
    if df_buffer is not None and not df_buffer.empty:
        b = df_buffer.copy()
        b["Jam Target"] = "-"
        b["Bergeser?"] = "-"
        frames.append(b[["Operasi", "Ruangan", "Start_DT", "Finish_DT",
                         "Prioritas", "Dokter", "Tindakan", "Waktu",
                         "Durasi (jam)", "Jam Target", "Bergeser?",
                         "Skor Klinis"]].copy())

    df_all = pd.concat(frames, ignore_index=True)

    fig = px.timeline(
        df_all, x_start="Start_DT", x_end="Finish_DT",
        y="Ruangan", color="Operasi", text="Operasi",
        color_discrete_map=PETA_WARNA,
        category_orders={"Ruangan": cfg["RUANGAN"]},
        custom_data=["Tindakan", "Dokter", "Waktu", "Durasi (jam)",
                     "Prioritas", "Jam Target", "Bergeser?", "Skor Klinis"],
    )
    fig.update_yaxes(autorange="reversed", tickfont=dict(size=13))
    fig.update_traces(
        textposition="inside", insidetextanchor="middle",
        textfont=dict(size=11, color="white"),
        hovertemplate=(
            "<b>%{customdata[0]}</b><br>"
            "Dokter: %{customdata[1]}<br>"
            "Kamar: %{y}<br>"
            "Waktu: %{customdata[2]}<br>"
            "Durasi: %{customdata[3]} jam<br>"
            "Prioritas: %{customdata[4]}<br>"
            "Jam diminta: %{customdata[5]}<br>"
            "Bergeser: %{customdata[6]}<br>"
            "Skor klinis: %{customdata[7]}"
            "<extra></extra>"
        ),
    )
    tickvals = [tgl_obj.replace(hour=h) for h in range(cfg["JAM_START"], cfg["JAM_END"] + 1)]
    ticktext = [f"{h:02d}:00" for h in range(cfg["JAM_START"], cfg["JAM_END"] + 1)]
    fig.update_xaxes(tickvals=tickvals, ticktext=ticktext, tickangle=-45,
                     tickfont=dict(size=11), gridcolor="#e8ecf0")
    fig.update_layout(
        title="Jadwal Operasi", xaxis_title="", yaxis_title="",
        legend_title="Jadwal Operasi", height=320,
        margin=dict(l=0, r=0, t=30, b=0),
        plot_bgcolor="#f8fafc", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True, key=key)


# =====================================================================
# 7. CSS
# =====================================================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.block-container { padding: 1rem 1.8rem 2rem 1.8rem !important; }

[data-testid="stSidebar"] { background: linear-gradient(180deg,#0d1b2a 0%,#1b3a5c 100%) !important; }
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] div,
[data-testid="stSidebar"] .stMarkdown,
[data-testid="stSidebar"] .stCaption  { color: #e2e8f0 !important; }
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3          { color: #90caf9 !important; }
[data-testid="stSidebar"] hr          { border-color: #2a4a6b !important; }
[data-testid="stSidebar"] input       { background:#1e3a5f !important; color:#e2e8f0 !important; border-color:#3a5a7a !important; }

.stTabs [data-baseweb="tab-list"] { background:#f0f4f8; border-radius:12px; padding:4px; gap:4px; }
.stTabs [data-baseweb="tab"]      { font-size:13px; font-weight:600; border-radius:8px; padding:8px 16px; color:#4a5568; }
.stTabs [aria-selected="true"]    { background:white !important; color:#1a56db !important; box-shadow:0 1px 3px rgba(0,0,0,.1); }

[data-testid="stMetric"]       { background:white; border:1px solid #e2e8f0; border-radius:12px; padding:14px 18px; box-shadow:0 1px 4px rgba(0,0,0,.06); }
[data-testid="stMetricLabel"]  { font-size:11px !important; color:#64748b !important; font-weight:600 !important; text-transform:uppercase; letter-spacing:.04em; }
[data-testid="stMetricValue"]  { font-size:24px !important; font-weight:700 !important; color:#1e293b !important; }

.card         { background:white; border-radius:12px; padding:14px 18px; margin-bottom:10px; box-shadow:0 1px 4px rgba(0,0,0,.06); border:1px solid #e2e8f0; }
.card-blue    { border-left:4px solid #1a56db; }
.card-orange  { border-left:4px solid #f59e0b; }
.card-red     { border-left:4px solid #ef4444; }
.card-green   { border-left:4px solid #10b981; }
.card-gray    { border-left:4px solid #94a3b8; }

.badge-cito    { background:#fee2e2; color:#dc2626; border-radius:5px; padding:2px 8px; font-size:11px; font-weight:700; }
.badge-elektif { background:#dbeafe; color:#1d4ed8; border-radius:5px; padding:2px 8px; font-size:11px; font-weight:700; }
.badge-geser   { background:#fef3c7; color:#b45309; border-radius:5px; padding:2px 8px; font-size:11px; font-weight:700; }

.section-header { font-size:12px; font-weight:700; color:#475569; text-transform:uppercase;
    letter-spacing:.06em; margin:0 0 10px 0; padding-bottom:8px; border-bottom:2px solid #e2e8f0; }
.gantt-wrapper  { background:white; border-radius:12px; padding:14px 16px; margin-bottom:12px;
    box-shadow:0 1px 4px rgba(0,0,0,.06); border:1px solid #e2e8f0; }
.info-box { background:#eff6ff; border:1px solid #bfdbfe; border-radius:10px; padding:10px 14px;
    margin-bottom:10px; font-size:13px; color:#1e40af; }

.stButton > button[kind="primary"] {
    background:linear-gradient(135deg,#1a56db,#1e40af) !important;
    color:white !important; border:none !important;
    border-radius:8px !important; font-weight:600 !important;
    box-shadow:0 2px 6px rgba(26,86,219,.3) !important;
}
.stButton > button[kind="primary"]:hover { transform:translateY(-1px); }
</style>
""", unsafe_allow_html=True)

# =====================================================================
# 8. SIDEBAR (BAHASA RAMAH PENGGUNA)
# =====================================================================
with st.sidebar:
    st.markdown("## 🏥 Kamar Operasi")
    st.markdown("Instalasi Bedah Sentral · RS Polri")
    st.markdown("---")

    st.markdown("### 📅 Pengaturan Hari Operasi")
    tgl_input = st.date_input("Tanggal jadwal operasi", datetime.now())
    tgl_str = tgl_input.strftime("%Y-%m-%d")
    jam_skrg = st.slider(
        "Jam berjalan saat ini", 7, 22, 7,
        help="Operasi yang sudah lewat jam ini akan dikunci dan tidak diubah lagi.",
    )

    with st.expander("⚙️ Pengaturan lanjutan (opsional)", expanded=False):
        st.markdown("**Jeda istirahat / pergantian sesi**")
        ji_start = st.number_input("Istirahat boleh mulai jam", 7, 22,
                                   value=st.session_state.CONFIG["JAM_ISTIRAHAT_START"])
        ji_max = st.number_input("Istirahat paling lambat jam", 7, 22,
                                 value=st.session_state.CONFIG["JAM_ISTIRAHAT_MAX"])
        ji_dur = st.number_input("Lama istirahat (jam)", 1, 3,
                                 value=st.session_state.CONFIG["DURASI_ISTIRAHAT"])

        st.markdown("**Seberapa 'mahal' mengubah jadwal**")
        p_geser = st.number_input("Penalti menggeser waktu (per jam)", 0, 1000,
                                  value=st.session_state.CONFIG["PENALTI_GESER_JAM"], step=10)
        p_pindah = st.number_input("Penalti pindah kamar", 0, 1000,
                                   value=st.session_state.CONFIG["PENALTI_PINDAH_OK"], step=10)
        p_ok3 = st.number_input("Penalti memakai OK 3 (kamar cadangan)", 0, 2000,
                                value=st.session_state.CONFIG["PENALTI_PAKAI_OK3"], step=50)

        st.markdown("**Bobot tingkat kepentingan pasien**")
        w_urg = st.number_input("Bobot kasus darurat", 100, 5000,
                                value=st.session_state.CONFIG["BOBOT_URGENCY"], step=100)
        w_asa = st.number_input("Bobot kondisi fisik (ASA)", 0, 500,
                                value=st.session_state.CONFIG["BOBOT_ASA"], step=10)
        w_klas = st.number_input("Bobot tingkat cedera", 0, 200,
                                 value=st.session_state.CONFIG["BOBOT_KLASIFIKASI"], step=10)
        w_imp = st.number_input("Bobot pemakaian implan", 0, 200,
                                value=st.session_state.CONFIG["BOBOT_IMPLAN"], step=5)

        st.session_state.CONFIG.update({
            "JAM_ISTIRAHAT_START": int(ji_start),
            "JAM_ISTIRAHAT_MAX": int(ji_max),
            "DURASI_ISTIRAHAT": int(ji_dur),
            "PENALTI_GESER_JAM": int(p_geser),
            "PENALTI_PINDAH_OK": int(p_pindah),
            "PENALTI_PAKAI_OK3": int(p_ok3),
            "BOBOT_URGENCY": int(w_urg),
            "BOBOT_ASA": int(w_asa),
            "BOBOT_KLASIFIKASI": int(w_klas),
            "BOBOT_IMPLAN": int(w_imp),
        })

    st.markdown("---")
    st.caption("Sistem otomatis menyusun jadwal kamar operasi\n"
               "dengan mempertimbangkan prioritas pasien,\n"
               "ketersediaan kamar & dokter, serta jeda istirahat.")

# =====================================================================
# 9. INIT SESI + HEADER
# =====================================================================
sesi = init_sesi(tgl_str)
cfg = st.session_state.CONFIG

st.markdown(f"""
<h1 style="margin:0;font-size:20px;font-weight:700;color:#1e293b;letter-spacing:-0.01em;">
    Sistem Penjadwalan Kamar Operasi — Instalasi Bedah Sentral
</h1>
<p style="margin:3px 0 10px 0;color:#64748b;font-size:13px;">
    RS Polri &nbsp;|&nbsp; Tanggal: <b>{tgl_str}</b>
    &nbsp;|&nbsp; Jam berjalan: <b>{jam_skrg:02d}:00</b>
</p>
""", unsafe_allow_html=True)

df_head = sesi.get("df_jadwal")
ada_jadwal = isinstance(df_head, pd.DataFrame) and not df_head.empty
jml = len(df_head) if ada_jadwal else 0

if ada_jadwal:
    n_darurat = int((df_head["Prioritas"] == "🚨 Darurat").sum())
    n_kamar = int(df_head["Ruangan"].nunique())
    total_jam = int(df_head["Durasi (jam)"].sum())
    n_gagal = len(sesi.get("gagal", []))
else:
    n_darurat = n_kamar = total_jam = n_gagal = 0

mc1, mc2, mc3, mc4, mc5 = st.columns(5)
mc1.metric("Operasi Terjadwal", jml if ada_jadwal else "—")
mc2.metric("Operasi Darurat", n_darurat if ada_jadwal else "—")
mc3.metric("Kamar Terpakai", f"{n_kamar} kamar" if ada_jadwal else "—")
mc4.metric("Total Jam Operasi", f"{total_jam} jam" if ada_jadwal else "—")
mc5.metric("Belum Terjadwal", n_gagal if ada_jadwal else "—")
st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

# =====================================================================
# 10. TABS
# =====================================================================
tab1, tab2, tab3, tab4 = st.tabs([
    "📝 Daftar Pasien",
    "📊 Jadwal & Penyesuaian",
    "✏️ Ubah Manual",
    "📋 Rincian & Laporan",
])

# ══════════════════════════════════════════════════════════════════
# TAB 1 — DAFTAR PASIEN
# ══════════════════════════════════════════════════════════════════
with tab1:
    col_form, col_list = st.columns([1, 1], gap="large")

    with col_form:
        st.markdown('<p class="section-header">➕ Tambah Pasien</p>', unsafe_allow_html=True)
        st.markdown(f'<div class="info-box">📌 Pasien akan masuk ke antrean tanggal <b>{tgl_str}</b>. '
                    'Isi data di bawah lalu tekan tombol tambah.</div>', unsafe_allow_html=True)

        with st.form("form_input", clear_on_submit=True):
            p_id = st.number_input("Nomor urut pasien", min_value=1, step=1,
                                   value=max([p['id'] for p in sesi["semua_pasien"]], default=0) + 1)
            p_tind = st.text_input("Jenis operasi / diagnosa",
                                   placeholder="Contoh: ORIF Femur Kiri")
            p_dok = st.selectbox("Dokter operator", options=DAFTAR_DOKTER)
            st.markdown("---")

            ca, cb = st.columns(2)
            with ca:
                p_emg = st.radio("Tingkat urgensi",
                                 options=[0, 1],
                                 format_func=lambda x: "🚨 Darurat (CITO)" if x == 1 else "📅 Terjadwal (elektif)")
                p_jam = st.number_input("Jam mulai yang diinginkan", min_value=7, max_value=22, value=7, step=1)
                p_carm = st.radio("Perlu alat C-Arm?", options=[0, 1],
                                  format_func=lambda x: "Ya" if x == 1 else "Tidak",
                                  horizontal=True)
                p_imp = st.radio("Pakai implan / pen?", options=[0, 1],
                                 format_func=lambda x: "Ya" if x == 1 else "Tidak",
                                 horizontal=True)
            with cb:
                st.markdown("**Perkiraan lama operasi (jam)**")
                d_min = st.number_input("Tercepat", min_value=1, max_value=5, value=1, step=1)
                d_avg = st.number_input("Rata-rata", min_value=1, max_value=8, value=2, step=1)
                d_max = st.number_input("Terlama", min_value=1, max_value=10, value=3, step=1)
                asa_lbl = st.selectbox("Kondisi fisik pasien (ASA)", options=list(PETA_ASA.keys()))
                klas_lbl = st.selectbox("Tingkat keparahan cedera", options=list(PETA_KLAS.keys()))

            submitted = st.form_submit_button("➕ Tambahkan pasien", use_container_width=True, type="primary")
            if submitted:
                p_asa = PETA_ASA[asa_lbl]
                p_klas = PETA_KLAS[klas_lbl]
                errs = []
                if not p_tind.strip():
                    errs.append("Mohon isi jenis operasi / diagnosa.")
                if d_min > d_avg:
                    errs.append("Durasi tercepat tidak boleh lebih besar dari rata-rata.")
                if d_avg > d_max:
                    errs.append("Durasi rata-rata tidak boleh lebih besar dari terlama.")
                if int(p_id) in [p['id'] for p in sesi["semua_pasien"]]:
                    errs.append(f"Nomor {int(p_id)} sudah dipakai.")
                if errs:
                    for e in errs:
                        st.error(e)
                else:
                    pasien_baru = {
                        "id": int(p_id), "tindakan": p_tind.strip(),
                        "emergency": int(p_emg), "pref_start": int(p_jam),
                        "d_min": int(d_min), "d_avg": int(d_avg), "d_max": int(d_max),
                        "c_arm": int(p_carm), "dokter": p_dok,
                        "asa": int(p_asa), "klasifikasi": int(p_klas), "implan": int(p_imp),
                    }
                    ei_preview = hitung_ei(pasien_baru, cfg)
                    sesi["semua_pasien"].append(pasien_baru)
                    lbl = "🚨 Darurat" if int(p_emg) == 1 else "Terjadwal"
                    st.success(f"✅ Pasien {int(p_id)} ({lbl}) — {p_tind} berhasil ditambahkan "
                               f"(skor kepentingan {ei_preview:,}).")

    with col_list:
        st.markdown('<p class="section-header">📋 Antrean Pasien Hari Ini</p>', unsafe_allow_html=True)
        if sesi["semua_pasien"]:
            for idx_p, p in enumerate(sesi["semua_pasien"]):
                lbl_p = "🚨 Darurat" if p['emergency'] == 1 else "Terjadwal"
                c_badge = "#fee2e2" if p['emergency'] == 1 else "#dbeafe"
                t_badge = "#dc2626" if p['emergency'] == 1 else "#1d4ed8"
                ei_val = hitung_ei(p, cfg)
                col_info, col_del = st.columns([9, 1])
                with col_info:
                    st.markdown(f"""
                    <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;
                         padding:8px 12px;margin-bottom:4px;font-size:13px;">
                        <span style="background:{c_badge};color:{t_badge};border-radius:4px;
                              padding:1px 7px;font-size:11px;font-weight:700;margin-right:6px;">{lbl_p}</span>
                        <b>No. {p['id']}</b> — {p['tindakan']} &nbsp;|&nbsp; {p['dokter']}<br>
                        <span style="color:#64748b;font-size:12px">
                        Diminta: {p['pref_start']:02d}:00 &nbsp;·&nbsp;
                        Durasi: {p['d_min']}–{p['d_avg']}–{p['d_max']} jam &nbsp;·&nbsp;
                        C-Arm: {"Ya" if p['c_arm'] == 1 else "Tidak"} &nbsp;·&nbsp;
                        ASA: {p.get('asa', 1)} &nbsp;·&nbsp;
                        Cedera grade: {p.get('klasifikasi', 1)} &nbsp;·&nbsp;
                        Implan: {"Ya" if p.get('implan', 0) == 1 else "Tidak"} &nbsp;·&nbsp;
                        <b>Skor: {ei_val:,}</b>
                        </span>
                    </div>
                    """, unsafe_allow_html=True)
                with col_del:
                    if st.button("✕", key=f"del_p_{idx_p}", help=f"Hapus pasien {p['id']}"):
                        sesi["semua_pasien"].pop(idx_p)
                        st.rerun()

            st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
            cx, cy = st.columns(2)
            with cx:
                if st.button("🗑️ Kosongkan antrean", type="secondary", use_container_width=True):
                    sesi.update({"semua_pasien": [], "df_jadwal": None, "df_buffer": None,
                                 "memori_terkunci": {}, "gagal": [], "log_edit": [],
                                 "log_batal": [], "trace_log": [], "z_score": None,
                                 "total_reward": None, "total_penalti": None, "status_solver": None})
                    st.rerun()
            with cy:
                if st.button("▶️ Susun Jadwal Otomatis", type="primary", use_container_width=True):
                    with st.spinner("Sistem sedang menyusun jadwal terbaik..."):
                        df_baru, gagal, Z, status = run_optimasi(sesi, cfg, jam_skrg, tgl_str)
                    sesi["log_edit"] = []
                    if status != "Optimal":
                        st.error("⚠️ Jadwal tidak bisa disusun — kemungkinan jumlah operasi melebihi "
                                 "kapasitas ketiga kamar. Coba kurangi pasien atau longgarkan pengaturan.")
                    else:
                        if gagal:
                            st.warning(f"⚠️ Pasien nomor {gagal} belum bisa dijadwalkan hari ini.")
                        st.success(f"✅ {len(df_baru)} operasi berhasil dijadwalkan.")
                    st.rerun()
        else:
            st.info("Belum ada pasien. Tambahkan pasien melalui form di sebelah kiri.")

# ══════════════════════════════════════════════════════════════════
# TAB 2 — JADWAL & PENYESUAIAN
# ══════════════════════════════════════════════════════════════════
with tab2:
    df_j = sesi.get("df_jadwal")
    df_buf = sesi.get("df_buffer")
    has_jadwal = df_j is not None and isinstance(df_j, pd.DataFrame) and not df_j.empty

    if not has_jadwal:
        st.info("Belum ada jadwal. Susun jadwal dulu di tab 📝 Daftar Pasien.")
    else:
        st.markdown('<div class="gantt-wrapper">', unsafe_allow_html=True)
        st.markdown('<p class="section-header">🗓️ Tampilan Jadwal Kamar Operasi</p>',
                    unsafe_allow_html=True)

        geser_auto = df_j[df_j["Bergeser?"] == "Ya"]
        if not geser_auto.empty:
            with st.expander(f"⚠️ {len(geser_auto)} operasi digeser dari jam yang diminta"):
                for _, row in geser_auto.iterrows():
                    badge = '<span class="badge-cito">Darurat</span>' if row['Prioritas'] == "🚨 Darurat" else '<span class="badge-elektif">Terjadwal</span>'
                    st.markdown(f"""<div class="card card-orange" style="padding:8px 14px">
                        {badge} &nbsp;<b>{row['Operasi']} — {row['Tindakan']}</b> ({row['Dokter']})<br>
                        Diminta: <b>{row['Jam Target']}</b> → Dijadwalkan: <b>{row['Waktu']}</b>
                        di <b>{row['Ruangan']}</b>
                    </div>""", unsafe_allow_html=True)

        buat_gantt(df_j, df_buf, tgl_str, cfg, key="gantt_tab2_main")
        st.markdown('</div>', unsafe_allow_html=True)

        cols_show = ["Operasi", "Prioritas", "Dokter", "Tindakan", "Ruangan", "Waktu",
                     "Durasi (jam)", "C-Arm", "Jam Target", "Bergeser?",
                     "Skor Klinis", "ASA", "Klasifikasi", "Implan", "Catatan Durasi"]
        st.dataframe(df_j[cols_show].sort_values(["Ruangan", "Waktu"]),
                     use_container_width=True, hide_index=True)

        st.markdown("---")
        st.markdown('<p class="section-header">🔄 Penyesuaian Jadwal</p>', unsafe_allow_html=True)

        sc_b, sc_c = st.columns(2, gap="large")

        # ── Batalkan Operasi ──
        with sc_b:
            st.markdown("""<div class="card card-orange">
                <b>🚫 Batalkan Operasi</b><br>
                <small>Pilih operasi → batalkan → sistem menyusun ulang otomatis.</small>
            </div>""", unsafe_allow_html=True)

            id_opts_b = sorted(df_j["id_num"].unique().tolist())
            lbl_b = [f"No. {i} — {df_j[df_j['id_num'] == i]['Tindakan'].values[0]} ({df_j[df_j['id_num'] == i]['Dokter'].values[0]})"
                     for i in id_opts_b]
            map_b = dict(zip(lbl_b, id_opts_b))
            sel_b = st.selectbox("Pilih operasi yang dibatalkan", options=lbl_b, key="sel_batal")
            pid_b = map_b[sel_b]
            baris_b = df_j[df_j["id_num"] == pid_b].iloc[0]
            st.markdown(f"""<div class="card card-gray" style="padding:8px 14px;font-size:13px">
                <b>Jadwal saat ini:</b> {baris_b['Ruangan']} · {baris_b['Waktu']} · {baris_b['Durasi (jam)']} jam
            </div>""", unsafe_allow_html=True)

            if st.button("🚫 Batalkan & Susun Ulang", type="primary",
                         use_container_width=True, key="btn_batal"):
                sesi["log_batal"].append({
                    "waktu": datetime.now().strftime("%H:%M:%S"),
                    "id": pid_b, "tindakan": baris_b['Tindakan'],
                    "dokter": baris_b['Dokter'], "ruangan": baris_b['Ruangan'],
                    "waktu_op": baris_b['Waktu'], "durasi": baris_b['Durasi (jam)'],
                    "prioritas": baris_b['Prioritas'], "alasan": "Pembatalan manual",
                })
                sesi["semua_pasien"] = [p for p in sesi["semua_pasien"] if p['id'] != pid_b]
                sesi["memori_terkunci"].pop(pid_b, None)
                with st.spinner("Menyusun ulang jadwal..."):
                    if sesi["semua_pasien"]:
                        df_baru, gagal, Z, status = run_optimasi(sesi, cfg, jam_skrg, tgl_str)
                    else:
                        sesi.update({"df_jadwal": pd.DataFrame(), "df_buffer": pd.DataFrame(),
                                     "gagal": [], "trace_log": [], "z_score": 0,
                                     "total_reward": 0, "total_penalti": 0, "status_solver": "Optimal"})
                        Z = 0
                sesi["log_edit"].append({"waktu": datetime.now().strftime("%H:%M:%S"),
                                         "jenis": "🚫 Pembatalan", "detail": f"No. {pid_b} dibatalkan"})
                st.success(f"✅ Operasi nomor {pid_b} dibatalkan. Jadwal telah disusun ulang.")
                st.rerun()

        # ── Geser Jadwal ──
        with sc_c:
            st.markdown("""<div class="card card-blue">
                <b>🔀 Geser Jam Operasi</b><br>
                <small>Ubah jam yang diinginkan → sistem menyusun ulang otomatis.</small>
            </div>""", unsafe_allow_html=True)

            id_opts_c = sorted(df_j["id_num"].unique().tolist())
            lbl_c = [f"No. {i} — {df_j[df_j['id_num'] == i]['Tindakan'].values[0]} ({df_j[df_j['id_num'] == i]['Dokter'].values[0]})"
                     for i in id_opts_c]
            map_c = dict(zip(lbl_c, id_opts_c))
            sel_c = st.selectbox("Pilih operasi", options=lbl_c, key="sel_geser")
            pid_c = map_c[sel_c]
            baris_c = df_j[df_j["id_num"] == pid_c].iloc[0]
            jam_c_now = int(baris_c['Waktu'].split(":")[0])
            st.markdown(f"""<div class="card card-gray" style="padding:8px 14px;font-size:13px">
                <b>Jadwal saat ini:</b> {baris_c['Ruangan']} · {baris_c['Waktu']} · {baris_c['Durasi (jam)']} jam
            </div>""", unsafe_allow_html=True)

            jam_c = st.number_input("Jam baru yang diinginkan", min_value=jam_skrg, max_value=22,
                                    value=jam_c_now, step=1, key="jam_geser")

            if st.button("🔀 Geser & Susun Ulang", type="primary",
                         use_container_width=True, key="btn_geser"):
                for p in sesi["semua_pasien"]:
                    if p['id'] == pid_c:
                        p['pref_start'] = int(jam_c)
                        break
                with st.spinner("Menyusun ulang jadwal..."):
                    df_baru, gagal, Z, status = run_optimasi(sesi, cfg, jam_skrg, tgl_str)
                sesi["log_edit"].append({"waktu": datetime.now().strftime("%H:%M:%S"),
                                         "jenis": "🔀 Geser jam",
                                         "detail": f"No. {pid_c} → diminta jam {jam_c:02d}:00"})
                st.success(f"✅ Operasi nomor {pid_c} digeser ke {jam_c:02d}:00. Jadwal telah disusun ulang.")
                st.rerun()

        # Riwayat Perubahan
        log = sesi.get("log_edit", [])
        if log:
            st.markdown("---")
            st.markdown('<p class="section-header">📜 Riwayat Perubahan</p>', unsafe_allow_html=True)
            for entry in reversed(log[-8:]):
                st.markdown(f"""<div class="card card-green"
                    style="padding:8px 14px;margin-bottom:4px;font-size:13px">
                    <span style="color:#6b7280;font-size:11px">{entry['waktu']}</span> &nbsp;
                    <b>{entry['jenis']}</b> — {entry['detail']}
                </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════
# TAB 3 — UBAH MANUAL
# ══════════════════════════════════════════════════════════════════
with tab3:
    st.markdown('<p class="section-header">✏️ Ubah Jadwal Secara Manual</p>', unsafe_allow_html=True)
    df_edit = sesi.get("df_jadwal")
    df_buf_edit = sesi.get("df_buffer")

    if df_edit is None or (isinstance(df_edit, pd.DataFrame) and df_edit.empty):
        st.info("Belum ada jadwal aktif. Susun jadwal dulu di tab 📝 Daftar Pasien.")
    else:
        with st.expander("👁️ Lihat tampilan jadwal saat ini", expanded=False):
            buat_gantt(df_edit, df_buf_edit, tgl_str, cfg, key="gantt_tab3_preview")

        e1, e2 = st.columns([1, 1], gap="large")

        with e1:
            id_opts_e = sorted(df_edit["id_num"].unique().tolist())
            lbl_e = [f"No. {i}  |  {df_edit[df_edit['id_num'] == i]['Tindakan'].values[0]}"
                     f"  |  {df_edit[df_edit['id_num'] == i]['Dokter'].values[0]}"
                     for i in id_opts_e]
            map_e = dict(zip(lbl_e, id_opts_e))
            pilihan = st.selectbox("Pilih operasi", options=lbl_e, key="edit_sel_pasien")
            pid_edit = map_e[pilihan]
            baris = df_edit[df_edit["id_num"] == pid_edit].iloc[0]

            card_cls = "card-red" if baris['Prioritas'] == "🚨 Darurat" else "card-blue"
            badge = '<span class="badge-cito">Darurat</span>' if baris['Prioritas'] == "🚨 Darurat" else '<span class="badge-elektif">Terjadwal</span>'
            geser_badge = '<span class="badge-geser">⚠️ Digeser otomatis</span>' if baris["Bergeser?"] == "Ya" else ""
            st.markdown(f"""
            <div class="card {card_cls}">
                {badge} {geser_badge} &nbsp; <b>No. {pid_edit} — {baris['Tindakan']}</b><br><br>
                🏥 <b>Kamar:</b> {baris['Ruangan']} &nbsp;|&nbsp;
                🕐 <b>Waktu:</b> {baris['Waktu']} &nbsp;|&nbsp;
                ⏱️ <b>Durasi:</b> {baris['Durasi (jam)']} jam<br>
                👨‍⚕️ <b>Dokter:</b> {baris['Dokter']} &nbsp;|&nbsp;
                🎯 <b>Jam diminta:</b> {baris['Jam Target']} &nbsp;|&nbsp;
                🦾 <b>C-Arm:</b> {baris['C-Arm']}<br>
                🧬 <b>Skor klinis:</b> {baris['Skor Klinis']} &nbsp;|&nbsp;
                🩺 <b>ASA:</b> {baris['ASA']} &nbsp;|&nbsp;
                🦴 <b>Cedera:</b> {baris['Klasifikasi']} &nbsp;|&nbsp;
                🔩 <b>Implan:</b> {baris['Implan']}
            </div>""", unsafe_allow_html=True)

        with e2:
            jam_saat_ini = int(baris['Waktu'].split(":")[0])
            durasi_ini = int(baris['Durasi (jam)'])
            ruangan_ini = baris['Ruangan']
            carm_pasien = 1 if baris['C-Arm'] == 'Ya' else 0
            ruangan_valid = [r for r in cfg["RUANGAN"]
                             if not (carm_pasien == 1 and r in cfg["RUANGAN_TANPA_CARM"])]

            ruangan_baru = st.selectbox(
                "Pindah ke kamar", options=ruangan_valid,
                index=ruangan_valid.index(ruangan_ini) if ruangan_ini in ruangan_valid else 0,
                key="edit_ruangan_sel",
            )
            max_jam = cfg["JAM_END"] - durasi_ini
            jam_baru = st.number_input(
                "Jam mulai baru", min_value=jam_skrg, max_value=max_jam,
                value=min(jam_saat_ini, max_jam), step=1, key="edit_jam_inp",
            )

            tidak_berubah = (ruangan_baru == ruangan_ini and jam_baru == jam_saat_ini)

            if not tidak_berubah:
                st.markdown(f"""<div class="card card-orange" style="padding:10px 14px">
                    📌 <b>Perubahan yang akan diterapkan:</b><br>
                    Kamar: <b>{ruangan_ini}</b> → <b>{ruangan_baru}</b><br>
                    Waktu: <b>{jam_saat_ini:02d}:00–{jam_saat_ini + durasi_ini:02d}:00</b>
                    → <b>{jam_baru:02d}:00–{jam_baru + durasi_ini:02d}:00</b>
                </div>""", unsafe_allow_html=True)

            df_lain = df_edit[df_edit["id_num"] != pid_edit]
            slot_baru = set(range(jam_baru, jam_baru + durasi_ini))
            konflik = []
            for _, rl in df_lain[df_lain["Ruangan"] == ruangan_baru].iterrows():
                jl = int(rl['Waktu'].split(":")[0])
                dl = int(rl['Durasi (jam)'])
                if slot_baru & set(range(jl, jl + dl)):
                    konflik.append(f"No. {rl['id_num']} ({rl['Waktu']})")

            if konflik:
                st.error(f"⛔ Bentrok dengan: {', '.join(konflik)}")
            elif not tidak_berubah:
                st.success(f"✅ {jam_baru:02d}:00–{jam_baru + durasi_ini:02d}:00 di {ruangan_baru} tersedia.")
            else:
                st.info("ℹ️ Belum ada perubahan.")

            if st.button("💾 Simpan Perubahan", type="primary",
                         disabled=(bool(konflik) or tidak_berubah),
                         use_container_width=True, key="btn_simpan_edit"):
                tgl_obj = datetime.strptime(tgl_str, "%Y-%m-%d")
                idx = df_edit[df_edit["id_num"] == pid_edit].index[0]
                sesi["log_edit"].append({
                    "waktu": datetime.now().strftime("%H:%M:%S"),
                    "jenis": "✏️ Ubah manual",
                    "detail": f"No. {pid_edit}: {ruangan_ini}→{ruangan_baru}, {jam_saat_ini:02d}→{jam_baru:02d}:00",
                    "ruangan_lama": ruangan_ini, "waktu_lama": baris['Waktu'],
                    "ruangan_baru": ruangan_baru,
                    "waktu_baru": f"{jam_baru:02d}:00 - {jam_baru + durasi_ini:02d}:00",
                    "tindakan": baris['Tindakan'], "dokter": baris['Dokter'],
                })
                df_edit.at[idx, "Ruangan"] = ruangan_baru
                df_edit.at[idx, "Waktu"] = f"{jam_baru:02d}:00 - {jam_baru + durasi_ini:02d}:00"
                df_edit.at[idx, "Start_DT"] = tgl_obj.replace(hour=jam_baru)
                df_edit.at[idx, "Finish_DT"] = tgl_obj.replace(hour=jam_baru + durasi_ini)
                df_edit.at[idx, "Bergeser?"] = "—"
                sesi["df_jadwal"] = df_edit
                sesi["memori_terkunci"] = update_memori(df_edit)
                st.success("✅ Perubahan manual disimpan.")
                st.rerun()

        log_manual = [e for e in sesi.get("log_edit", []) if e.get("jenis") == "✏️ Ubah manual"]
        if log_manual:
            st.markdown("---")
            st.markdown('<p class="section-header">📜 Riwayat Perubahan Manual</p>', unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Jam": e["waktu"], "Detail": e["detail"],
                "Tindakan": e.get("tindakan", ""), "Dokter": e.get("dokter", ""),
                "Kamar Lama": e.get("ruangan_lama", ""), "Waktu Lama": e.get("waktu_lama", ""),
                "Kamar Baru": e.get("ruangan_baru", ""), "Waktu Baru": e.get("waktu_baru", ""),
            } for e in log_manual]), use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════
# TAB 4 — RINCIAN & LAPORAN
# ══════════════════════════════════════════════════════════════════
with tab4:
    t4a, t4b, t4c = st.tabs([
        "📋 Laporan Jadwal",
        "🗑️ Laporan Pembatalan",
        "🔬 Rincian Perhitungan",
    ])

    with t4a:
        st.markdown('<p class="section-header">📋 Laporan Jadwal Aktif</p>', unsafe_allow_html=True)
        df_rek = sesi.get("df_jadwal")
        if df_rek is None or (isinstance(df_rek, pd.DataFrame) and df_rek.empty):
            st.info("Belum ada jadwal.")
        else:
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Total Operasi", len(df_rek))
            r2.metric("Kamar Terpakai", df_rek["Ruangan"].nunique())
            r3.metric("Total Jam Operasi", f"{df_rek['Durasi (jam)'].sum()} jam")
            r4.metric("Belum Terjadwal", len(sesi.get("gagal", [])))
            st.markdown("---")
            cols_r = ["Operasi", "Prioritas", "Dokter", "Tindakan", "Ruangan", "Waktu",
                      "Durasi (jam)", "C-Arm", "Jam Target", "Bergeser?",
                      "Skor Klinis", "ASA", "Klasifikasi", "Implan", "Catatan Durasi"]
            st.dataframe(df_rek[cols_r].sort_values(["Ruangan", "Waktu"]),
                         use_container_width=True, hide_index=True)
            if sesi.get("gagal"):
                st.warning(f"⚠️ Belum terjadwal: pasien nomor {sesi['gagal']}")
            csv = df_rek[cols_r].to_csv(index=False)
            st.download_button("⬇️ Unduh Jadwal (CSV)", data=csv,
                               file_name=f"jadwal_operasi_{tgl_str}.csv",
                               mime="text/csv", use_container_width=True)

    with t4b:
        st.markdown('<p class="section-header">🗑️ Laporan Operasi Dibatalkan</p>', unsafe_allow_html=True)
        log_batal = sesi.get("log_batal", [])
        if not log_batal:
            st.info("Belum ada operasi yang dibatalkan.")
        else:
            st.markdown(f"""<div class="card card-orange">
                Total <b>{len(log_batal)}</b> operasi dibatalkan pada {tgl_str}.
            </div>""", unsafe_allow_html=True)
            for entry in log_batal:
                bp = '<span class="badge-cito">Darurat</span>' if entry['prioritas'] == "🚨 Darurat" else '<span class="badge-elektif">Terjadwal</span>'
                st.markdown(f"""<div class="card card-gray" style="padding:10px 14px;margin-bottom:6px">
                    <span style="color:#6b7280;font-size:11px">Dibatalkan: {entry['waktu']}</span><br>
                    {bp} &nbsp;<b>No. {entry['id']} — {entry['tindakan']}</b> · {entry['dokter']}<br>
                    🏥 {entry['ruangan']} · 🕐 {entry['waktu_op']} · ⏱️ {entry['durasi']} jam
                </div>""", unsafe_allow_html=True)

            df_batal = pd.DataFrame([{
                "Jam Batal": e["waktu"], "No.": e["id"], "Tindakan": e["tindakan"],
                "Dokter": e["dokter"], "Kamar": e["ruangan"],
                "Waktu Semula": e["waktu_op"], "Durasi": e["durasi"],
                "Prioritas": e["prioritas"], "Alasan": e["alasan"],
            } for e in log_batal])
            st.markdown("---")
            st.dataframe(df_batal, use_container_width=True, hide_index=True)
            csv_b = df_batal.to_csv(index=False)
            st.download_button("⬇️ Unduh Pembatalan (CSV)", data=csv_b,
                               file_name=f"pembatalan_operasi_{tgl_str}.csv",
                               mime="text/csv", use_container_width=True)

    with t4c:
        st.markdown('<p class="section-header">🔬 Rincian Perhitungan Jadwal</p>', unsafe_allow_html=True)
        st.caption("Bagian ini bersifat teknis — untuk keperluan verifikasi cara sistem "
                   "memilih jadwal. Tidak perlu dibaca untuk penggunaan sehari-hari.")
        trace = sesi.get("trace_log", [])
        if trace:
            Z = sesi.get("z_score", 0)
            R = sesi.get("total_reward", 0)
            P = sesi.get("total_penalti", 0)
            st.markdown(f"""<div class="card card-blue">
                <b>📊 Ringkasan Skor Jadwal</b><br><br>
                Skor akhir jadwal: <b>{Z:,.1f}</b><br>
                (+) Nilai positif (prioritas & jam ideal): <b>{R:,.1f}</b><br>
                (−) Pengurang (geser / pindah kamar / pakai OK 3): <b>{P:,.1f}</b><br>
                <br><b>Cara skor kepentingan pasien dihitung:</b><br>
                <small>Darurat ×{cfg['BOBOT_URGENCY']} + kondisi ASA ×{cfg['BOBOT_ASA']}
                + tingkat cedera ×{cfg['BOBOT_KLASIFIKASI']} + implan ×{cfg['BOBOT_IMPLAN']}</small><br>
                <small>Penalti: geser waktu {cfg['PENALTI_GESER_JAM']}/jam ·
                pindah kamar {cfg['PENALTI_PINDAH_OK']} · pakai OK 3 {cfg['PENALTI_PAKAI_OK3']}</small>
            </div>""", unsafe_allow_html=True)

            df_trace = pd.DataFrame(trace)

            def highlight_geser(row):
                if row.get('Geser (jam)', 0) != 0:
                    return ['background-color:#fef3c7'] * len(row)
                if row.get('Pengurang (Penalti)', 0) > 0:
                    return ['background-color:#fee2e2'] * len(row)
                return [''] * len(row)

            st.dataframe(df_trace.style.apply(highlight_geser, axis=1),
                         use_container_width=True, hide_index=True)

            geser_trace = [t for t in trace if t.get('Pengurang (Penalti)', 0) > 0]
            if geser_trace:
                st.markdown("---")
                st.markdown("**⚠️ Operasi yang terkena pengurang skor:**")
                for t in geser_trace:
                    st.markdown(f"""<div class="card card-orange"
                        style="padding:8px 14px;margin-bottom:4px">
                        <b>{t['Operasi']} — {t['Tindakan']}</b> → {t['Ruangan']} jam {t['Jam Dijadwalkan']}
                        &nbsp;|&nbsp; skor klinis {t.get('Skor Klinis (E_i)', '-')}<br>
                        Sebab: {t.get('Catatan', '—')} | Skor bersih: <b>{t['Skor Bersih']}</b>
                    </div>""", unsafe_allow_html=True)
        else:
            st.info("Rincian muncul setelah jadwal disusun.")