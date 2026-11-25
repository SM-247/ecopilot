import os
import sqlite3
import threading
import time
import uuid
import zlib
from datetime import datetime, timezone

import pandas as pd
import psutil
import requests
import streamlit as st
from llama_cpp import Llama

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
DB_PATH = "edge_energy_copilot.db"
CLOUD_BASE_URL = "http://127.0.0.1:8001"
MODEL_NAME = "Qwen2.5-0.5B-Instruct (GGUF Q4_K_M, 0.49B params)"
MODEL_PATH = "qwen2.5-0.5b-instruct-q4_k_m.gguf"

# Energy & Carbon Constants (Transparent Hardware & Grid Assumptions)
IDLE_WATTS = 6.0
TDP_WATTS = 28.0
RAM_WATTS_PER_GB = 0.3
TARIFF_INR_PER_KWH = 7.50
GRID_GCO2_PER_KWH = 675.0  # CEA India v22.0 Grid Emission Factor


# ==========================================
# DATABASE & DURABLE OUTBOX LAYER (SQLITE WAL)
# ==========================================
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            method TEXT,
            baseline_sec REAL,
            baseline_cpu REAL,
            baseline_wh REAL,
            baseline_inr REAL,
            baseline_co2 REAL,
            opt_sec REAL,
            opt_cpu REAL,
            opt_wh REAL,
            opt_inr REAL,
            opt_co2 REAL,
            wh_saved REAL,
            pct_saved REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER,
            created_at TEXT,
            model_name TEXT,
            mode TEXT,
            latency_sec REAL,
            inference_mwh REAL,
            summary_text TEXT,
            decision_status TEXT DEFAULT 'PENDING'
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT UNIQUE,
            entity_type TEXT,
            payload_json TEXT,
            status TEXT DEFAULT 'PENDING',
            retry_count INTEGER DEFAULT 0,
            next_retry_at REAL DEFAULT 0,
            created_at TEXT,
            synced_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def enqueue_sync(entity_type: str, payload: dict):
    import json

    conn = get_conn()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    idem_key = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sync_outbox (idempotency_key, entity_type, payload_json, status, retry_count, next_retry_at, created_at)
        VALUES (?, ?, ?, 'PENDING', 0, 0, ?)
        """,
        (idem_key, entity_type, json.dumps(payload), now_iso),
    )
    conn.commit()
    conn.close()


def background_sync_worker():
    while True:
        try:
            now_ts = time.time()
            conn = get_conn()
            rows = conn.execute(
                """
                SELECT * FROM sync_outbox
                WHERE status IN ('PENDING', 'FAILED') AND next_retry_at <= ?
                ORDER BY id ASC LIMIT 10
                """,
                (now_ts,),
            ).fetchall()

            for row in rows:
                try:
                    resp = requests.post(
                        f"{CLOUD_BASE_URL}/sync",
                        json={
                            "idempotency_key": row["idempotency_key"],
                            "entity_type": row["entity_type"],
                            "payload_json": row["payload_json"],
                            "created_at": row["created_at"],
                        },
                        timeout=1.5,
                    )
                    if resp.status_code == 200:
                        synced_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        conn.execute(
                            "UPDATE sync_outbox SET status='SYNCED', synced_at=? WHERE id=?",
                            (synced_ts, row["id"]),
                        )
                    else:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                except Exception:
                    retries = row["retry_count"] + 1
                    backoff = min(15, 2**retries)
                    conn.execute(
                        "UPDATE sync_outbox SET status='FAILED', retry_count=?, next_retry_at=? WHERE id=?",
                        (retries, time.time() + backoff, row["id"]),
                    )
            conn.commit()
            conn.close()
        except Exception:
            pass
        time.sleep(2.0)


@st.cache_resource
def start_sync_daemon():
    init_db()
    t = threading.Thread(target=background_sync_worker, daemon=True)
    t.start()
    return True


# ==========================================
# HARDWARE TELEMETRY & ENERGY MODELING
# ==========================================
def read_rapl_uj():
    rapl_path = "/sys/class/powercap/intel-rapl:0/energy_uj"
    try:
        if os.path.exists(rapl_path) and os.access(rapl_path, os.R_OK):
            with open(rapl_path, "r") as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None


def estimate_instant_watts(cpu_pct: float, ram_gb: float) -> float:
    return IDLE_WATTS + (TDP_WATTS - IDLE_WATTS) * ((cpu_pct / 100.0) ** 1.3) + (RAM_WATTS_PER_GB * ram_gb)


def measure_workload(fn):
    psutil.cpu_percent(interval=None)
    rapl_start = read_rapl_uj()
    t0 = time.perf_counter()

    fn()

    duration_sec = max(time.perf_counter() - t0, 0.01)
    avg_cpu = psutil.cpu_percent(interval=None)
    ram_gb = psutil.virtual_memory().used / (1024**3)
    rapl_end = read_rapl_uj()

    if rapl_start is not None and rapl_end is not None and rapl_end >= rapl_start:
        wh = (rapl_end - rapl_start) / 3_600_000_000.0
        method = "Hardware Intel RAPL (energy_uj)"
    else:
        watts = estimate_instant_watts(avg_cpu, ram_gb)
        wh = watts * (duration_sec / 3600.0)
        method = f"Calibrated TDP Model (Idle={IDLE_WATTS}W, TDP={TDP_WATTS}W)"

    inr = (wh / 1000.0) * TARIFF_INR_PER_KWH
    co2 = (wh / 1000.0) * GRID_GCO2_PER_KWH
    return {
        "sec": round(duration_sec, 3),
        "cpu": round(avg_cpu, 1),
        "wh": round(wh, 6),
        "inr": round(inr, 6),
        "co2": round(co2, 4),
        "method": method,
    }


def run_batching_benchmark():
    sample_blob = (b"edge_telemetry_packet_payload_" * 400)

    # Baseline: 200 unbatched individual compressions with tight polling
    def unbatched_work():
        for _ in range(200):
            _ = zlib.compress(sample_blob, level=6)
            target = time.perf_counter() + 0.008
            while time.perf_counter() < target:
                pass  # Busy-wait polling waste

    # Optimized: 4 coalesced batches of 50 with clean OS sleep
    def batched_work():
        batch_blob = sample_blob * 50
        for _ in range(4):
            _ = zlib.compress(batch_blob, level=6)
            time.sleep(0.05)

    base = measure_workload(unbatched_work)
    opt = measure_workload(batched_work)

    wh_saved = round(max(base["wh"] - opt["wh"], 0.0), 6)
    pct_saved = round((wh_saved / base["wh"]) * 100.0, 1) if base["wh"] > 0 else 0.0
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO experiments (
            created_at, method, baseline_sec, baseline_cpu, baseline_wh, baseline_inr, baseline_co2,
            opt_sec, opt_cpu, opt_wh, opt_inr, opt_co2, wh_saved, pct_saved
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso,
            base["method"],
            base["sec"],
            base["cpu"],
            base["wh"],
            base["inr"],
            base["co2"],
            opt["sec"],
            opt["cpu"],
            opt["wh"],
            opt["inr"],
            opt["co2"],
            wh_saved,
            pct_saved,
        ),
    )
    exp_id = cur.lastrowid
    conn.commit()
    conn.close()

    enqueue_sync(
        "experiment_benchmark",
        {
            "experiment_id": exp_id,
            "wh_saved": wh_saved,
            "pct_saved": pct_saved,
            "method": base["method"],
        },
    )


# ==========================================
# LOCAL SLM (<4B PARAMETERS VIA LLAMA.CPP)
# ==========================================
@st.cache_resource
def load_local_slm():
    if not os.path.exists(MODEL_PATH):
        return None
    return Llama(model_path=MODEL_PATH, n_ctx=1024, n_threads=4, verbose=False)


def ask_local_slm(exp: dict, mode: str = "eco") -> tuple[str, float, float]:
    llm = load_local_slm()
    if llm is None:
        raise FileNotFoundError(
            f"Local model file '{MODEL_PATH}' not found yet. Finish downloading the GGUF file into this folder first."
        )

    max_tokens = 140 if mode == "eco" else 240
    prompt = f"""<|im_start|>system
You are an Offline Edge Energy Copilot. Give a concise 3-bullet plain-language report grounded ONLY in the user's measured metrics.<|im_end|>
<|im_start|>user
Measurement Method: {exp['method']}
Baseline (Unbatched Polling): {exp['baseline_sec']}s, Avg CPU {exp['baseline_cpu']}%, {exp['baseline_wh']} Wh, Rs.{exp['baseline_inr']}, {exp['baseline_co2']} gCO2e
Optimized (Coalesced Batching): {exp['opt_sec']}s, Avg CPU {exp['opt_cpu']}%, {exp['opt_wh']} Wh, Rs.{exp['opt_inr']}, {exp['opt_co2']} gCO2e
Verified Savings: {exp['wh_saved']} Wh ({exp['pct_saved']}%)

Write 3 short bullets:
1. Root cause of energy waste in the baseline run.
2. Projected kWh, INR, and gCO2e saved across 10,000 daily runs.
3. One practical engineering trade-off of workload batching.<|im_end|>
<|im_start|>assistant
"""
    t0 = time.perf_counter()
    out = llm(prompt, max_tokens=max_tokens, temperature=0.2, stop=["<|im_end|>"])
    latency = round(time.perf_counter() - t0, 2)

    # Estimate inference energy cost on edge CPU (~14W active package draw)
    inference_mwh = round((14.0 * (latency / 3600.0)) * 1000.0, 4)
    return out["choices"][0]["text"].strip(), latency, inference_mwh


# ==========================================
# STREAMLIT UI
# ==========================================
st.set_page_config(page_title="Offline-First Edge Energy Copilot", page_icon="⚡", layout="wide")
start_sync_daemon()

# Check mock cloud connectivity
is_online = False
try:
    r = requests.get(f"{CLOUD_BASE_URL}/health", timeout=0.4)
    is_online = r.status_code == 200
except Exception:
    is_online = False

st.title("⚡ Offline-First Edge Energy Copilot")
st.caption(
    f"Local SLM: `{MODEL_NAME}` | Storage: Local SQLite (WAL) | Grid Factor: `{GRID_GCO2_PER_KWH} gCO2e/kWh` | Tariff: `₹{TARIFF_INR_PER_KWH}/kWh`"
)

# Top Telemetry Strip
cpu_now = psutil.cpu_percent(interval=0.1)
ram_now = psutil.virtual_memory().used / (1024**3)
watts_now = estimate_instant_watts(cpu_now, ram_now)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Live CPU Load", f"{cpu_now:.1f}%")
m2.metric("Live RAM Used", f"{ram_now:.2f} GB")
m3.metric("Est. Package Power", f"{watts_now:.1f} W")
m4.metric("Sync Link Status", "🟢 ONLINE" if is_online else "🔴 OFFLINE (Queued)")

with st.expander("📐 Measurement Methodology & Hardware Assumptions"):
    st.markdown(
        f"""
        - **Primary Telemetry:** Reads Linux hardware energy counters (`/sys/class/powercap/intel-rapl:0/energy_uj`) when accessible.
        - **Fallback Power Model:** Non-linear CPU + DRAM estimator: $P(t) = {IDLE_WATTS}\\text{{W}} + ({TDP_WATTS}\\text{{W}} - {IDLE_WATTS}\\text{{W}}) \\times (U_{{\\text{{cpu}}}}/100)^{{1.3}} + {RAM_WATTS_PER_GB} \\times \\text{{RAM}}_{{\\text{{GB}}}}$.
        - **Cost & Carbon Assumptions:** Indian national grid average **{GRID_GCO2_PER_KWH} gCO2e/kWh** (CEA v22.0) and commercial tariff **₹{TARIFF_INR_PER_KWH}/kWh**.
        - **Privacy & Edge Footprint:** 100% local GGUF inference (`<4B` parameter budget) with zero external telemetry leaves the node unless explicitly synced.
        """
    )

st.divider()

conn = get_conn()
latest_exp = conn.execute("SELECT * FROM experiments ORDER BY id DESC LIMIT 1").fetchone()
latest_rep = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
outbox_rows = conn.execute("SELECT * FROM sync_outbox ORDER BY id DESC LIMIT 15").fetchall()
conn.close()

col_left, col_right = st.columns(2)

# LEFT COLUMN: A/B ENERGY BENCHMARK
with col_left:
    st.subheader("1. Reproducible A/B Energy Benchmark")
    if st.button("▶ Run Baseline vs. Batched Workload (~5s)", type="primary"):
        with st.spinner("Measuring unbatched polling vs. coalesced batching..."):
            run_batching_benchmark()
        st.rerun()

    if latest_exp:
        exp = dict(latest_exp)
        st.success(
            f"**Verified Energy Savings: {exp['pct_saved']}% ({exp['wh_saved']} Wh saved per run)** | Method: `{exp['method']}`"
        )
        comp_df = pd.DataFrame(
            [
                {
                    "Run Profile": "Baseline (Unbatched Polling)",
                    "Duration (s)": exp["baseline_sec"],
                    "Avg CPU (%)": exp["baseline_cpu"],
                    "Energy (Wh)": exp["baseline_wh"],
                    "Cost (₹)": exp["baseline_inr"],
                    "Carbon (gCO2e)": exp["baseline_co2"],
                },
                {
                    "Run Profile": "Optimized (Coalesced Batching)",
                    "Duration (s)": exp["opt_sec"],
                    "Avg CPU (%)": exp["opt_cpu"],
                    "Energy (Wh)": exp["opt_wh"],
                    "Cost (₹)": exp["opt_inr"],
                    "Carbon (gCO2e)": exp["opt_co2"],
                },
            ]
        )
        st.dataframe(comp_df, use_container_width=True, hide_index=True)
    else:
        st.info("Click the button above to run your first reproducible energy benchmark.")

# RIGHT COLUMN: LOCAL SLM COPILOT
with col_right:
    st.subheader("2. Local SLM Plain-Language Copilot (<4B)")
    slm_mode = st.radio(
        "Inference Profile (Latency vs. Detail Trade-off):",
        ["eco", "deep"],
        format_func=lambda x: "Eco Mode (140 tokens, fast)" if x == "eco" else "Deep Audit Mode (240 tokens, detailed)",
        horizontal=True,
    )

    if st.button("🧠 Generate Offline SLM Audit & Recommendation", disabled=(latest_exp is None)):
        try:
            with st.spinner("Running local Qwen2.5-0.5B inference completely offline..."):
                summary, lat, inf_mwh = ask_local_slm(dict(latest_exp), mode=slm_mode)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO reports (experiment_id, created_at, model_name, mode, latency_sec, inference_mwh, summary_text, decision_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                (latest_exp["id"], now_iso, MODEL_NAME, slm_mode, lat, inf_mwh, summary),
            )
            rep_id = cur.lastrowid
            conn.commit()
            conn.close()

            enqueue_sync(
                "slm_insight_report",
                {
                    "report_id": rep_id,
                    "experiment_id": latest_exp["id"],
                    "model_name": MODEL_NAME,
                    "latency_sec": lat,
                    "inference_mwh": inf_mwh,
                },
            )
            st.rerun()
        except Exception as e:
            st.error(str(e))

    if latest_rep:
        rep = dict(latest_rep)
        st.caption(
            f"Generated in **{rep['latency_sec']}s** | Inference Overhead: **{rep['inference_mwh']} mWh** | Status: **{rep['decision_status']}**"
        )
        st.markdown(rep["summary_text"])

        if rep["decision_status"] == "PENDING":
            if st.button("✅ Approve & Apply Batching Policy"):
                conn = get_conn()
                conn.execute("UPDATE reports SET decision_status='APPROVED' WHERE id=?", (rep["id"],))
                conn.commit()
                conn.close()
                enqueue_sync(
                    "user_decision",
                    {"report_id": rep["id"], "decision": "APPROVED", "policy": "COALESCED_BATCHING"},
                )
                st.rerun()
        else:
            st.success("✅ Policy Approved locally & queued for durable cloud sync.")

st.divider()

# BOTTOM SECTION: DURABLE OUTBOX QUEUE
q_col1, q_col2 = st.columns([4, 1])
with q_col1:
    st.subheader("3. Durable Offline Persistence & Sync Outbox Queue")
with q_col2:
    if st.button("🔄 Refresh Queue Status"):
        st.rerun()

if outbox_rows:
    outbox_df = pd.DataFrame(
        [
            {
                "ID": r["id"],
                "Idempotency Key (UUID)": r["idempotency_key"],
                "Entity Type": r["entity_type"],
                "Status": r["status"],
                "Retries": r["retry_count"],
                "Created At": r["created_at"],
                "Synced At": r["synced_at"] or "—",
            }
            for r in outbox_rows
        ]
    )
    st.dataframe(outbox_df, use_container_width=True, hide_index=True)
else:
    st.info("Outbox queue is currently empty. Run a benchmark or generate an SLM report while offline to test durable persistence.")
