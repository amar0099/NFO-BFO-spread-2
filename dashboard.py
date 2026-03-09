# ─────────────────────────────────────────────
# dashboard.py — Single file, no external imports
# Run with: streamlit run dashboard.py
# ─────────────────────────────────────────────

import os
import base64
import pyotp
import requests
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import time
from datetime import date
from urllib.parse import parse_qs, urlparse
from plotly.subplots import make_subplots
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
from fyers_apiv3 import fyersModel

# ─────────────────────────────────────────────
# CONFIG — edit these or set as env variables
# ─────────────────────────────────────────────

CLIENT_ID       = os.environ.get("FYERS_CLIENT_ID",  "YOUR_APP_ID-100")
SECRET_KEY      = os.environ.get("FYERS_SECRET_KEY", "YOUR_SECRET_KEY")
TOKEN_FILE      = "access_token.txt"
REFRESH_SECONDS = 10

# ─────────────────────────────────────────────
# AUTO TOKEN (TOTP)
# ─────────────────────────────────────────────

def get_secret(key):
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, "")

def b64(value):
    return base64.b64encode(str(value).encode()).decode()

def generate_token():
    client_id  = get_secret("FYERS_CLIENT_ID")
    secret_key = get_secret("FYERS_SECRET_KEY")
    username   = get_secret("FYERS_USERNAME")
    pin        = get_secret("FYERS_PIN")
    totp_key   = get_secret("FYERS_TOTP_KEY")
    redirect_uri = "http://127.0.0.1:8080/"

    missing = [k for k, v in {
        "FYERS_CLIENT_ID": client_id, "FYERS_SECRET_KEY": secret_key,
        "FYERS_USERNAME": username, "FYERS_PIN": pin, "FYERS_TOTP_KEY": totp_key,
    }.items() if not v]
    if missing:
        return None, f"Missing credentials: {', '.join(missing)}"

    try:
        s = requests.Session()
        r1 = s.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
                    json={"fy_id": b64(username), "app_id": "2"}, timeout=10)
        try:
            r1d = r1.json()
        except Exception:
            return None, f"Step 1 bad response (status {r1.status_code}): {r1.text[:200]}"
        if r1d.get("s") != "ok":
            return None, f"Step 1 failed: {r1d}"

        totp_code = pyotp.TOTP(totp_key).now()
        r2 = s.post("https://api-t2.fyers.in/vagator/v2/verify_otp",
                    json={"request_key": r1d["request_key"], "otp": totp_code}, timeout=10)
        r2d = r2.json()
        if r2d.get("s") != "ok":
            return None, f"Step 2 failed: {r2d}"

        r3 = s.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
                    json={"request_key": r2d["request_key"], "identity_type": "pin", "identifier": b64(pin)}, timeout=10)
        r3d = r3.json()
        if r3d.get("s") != "ok":
            return None, f"Step 3 failed: {r3d}"

        app_id = client_id.split("-")[0]
        r4 = s.post("https://api-t1.fyers.in/api/v3/token", json={
            "fyers_id": username, "app_id": app_id, "redirect_uri": redirect_uri,
            "appType": "100", "code_challenge": "", "state": "sample",
            "scope": "", "nonce": "", "response_type": "code", "create_cookie": True
        }, headers={"Authorization": f"Bearer {r3d['data']['access_token']}"}, timeout=10)
        r4d = r4.json()
        if r4d.get("s") != "ok":
            return None, f"Step 4 failed: {r4d}"

        auth_code = parse_qs(urlparse(r4d["Url"]).query).get("auth_code", [None])[0]
        if not auth_code:
            return None, f"No auth_code in: {r4d}"

        session = fyersModel.SessionModel(
            client_id=client_id, secret_key=secret_key,
            redirect_uri=redirect_uri, response_type="code", grant_type="authorization_code"
        )
        session.set_token(auth_code)
        r5d = session.generate_token()
        token = r5d.get("access_token")
        if not token:
            return None, f"Step 5 failed: {r5d}"
        return token, None
    except Exception as e:
        return None, f"Exception: {str(e)}"

# ─────────────────────────────────────────────
# FYERS CLIENT
# ─────────────────────────────────────────────

def load_fyers_from_file():
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError("Token file not found")
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    return fyersModel.FyersModel(client_id=get_secret("FYERS_CLIENT_ID") or CLIENT_ID, token=token, log_path="")

@st.cache_resource
def get_shared_token():
    """Generates token once — shared across ALL browser sessions on this server."""
    # Try local token file first (local PC)
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    # Auto-generate via TOTP
    token, error = generate_token()
    if token:
        return token
    raise RuntimeError(f"Fyers login failed: {error}")

def get_fyers_client():
    try:
        token = get_shared_token()
        cid = get_secret("FYERS_CLIENT_ID") or CLIENT_ID
        return fyersModel.FyersModel(client_id=cid, token=token, log_path="")
    except Exception as e:
        st.error(f"❌ Login failed: {e}")
        return None

# ─────────────────────────────────────────────
# SYMBOL BUILDER
# ─────────────────────────────────────────────

def build_symbol(exchange, underlying, expiry, option_type, strike):
    ot = "CE" if option_type.upper() in ("C", "CE") else "PE"
    yy, mm, dd = expiry[0:2], expiry[2:4], expiry[4:6]
    expiry_fyers = yy + str(int(mm)) + dd
    return f"{exchange}:{underlying}{expiry_fyers}{strike}{ot}"

# ─────────────────────────────────────────────
# FETCH CANDLES
# ─────────────────────────────────────────────

def fetch_candles(fyers, symbol, interval, date_str=None):
    if date_str is None:
        date_str = date.today().strftime("%Y-%m-%d")
    response = fyers.history(data={
        "symbol": symbol, "resolution": str(interval),
        "date_format": "1", "range_from": date_str,
        "range_to": date_str, "cont_flag": "1"
    })
    if response.get("s") != "ok":
        return pd.DataFrame()
    df = pd.DataFrame(response["candles"], columns=["timestamp","open","high","low","close","volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s").dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return df.drop(columns=["timestamp"]).set_index("datetime")

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────

st.set_page_config(page_title="NFO/BFO Spread Terminal", page_icon="📊", layout="wide", initial_sidebar_state="collapsed")

# ── CSS ───────────────────────────────────────────────────────────────
def get_theme():
    return {
        "bg":        "#f0f4f8",
        "bg2":       "#ffffff",
        "sidebar":   "#e8edf5",
        "card":      "#ffffff",
        "card_bdr":  "#cbd5e1",
        "text":      "#0f172a",
        "text2":     "#475569",
        "text3":     "#94a3b8",
        "ce":        "#dc2626",
        "pe":        "#059669",
        "diff":      "#d97706",
        "accent":    "#0284c7",
        "divider":   "#e2e8f0",
        "plot_bg":   "#f8fafc",
        "grid":      "#e2e8f0",
    }

T = get_theme()

st.markdown(f"""
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
    :root {{
        --bg: {T["bg"]};
        --bg2: {T["bg2"]};
        --sidebar: {T["sidebar"]};
        --card: {T["card"]};
        --card-bdr: {T["card_bdr"]};
        --text: {T["text"]};
        --text2: {T["text2"]};
        --text3: {T["text3"]};
        --ce: {T["ce"]};
        --pe: {T["pe"]};
        --diff: {T["diff"]};
        --accent: {T["accent"]};
        --divider: {T["divider"]};
    }}

    * {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif !important; }}
    code, .mono {{ font-family: "Courier New", monospace !important; }}

    .stApp {{
        background: var(--bg) !important;
        color: var(--text) !important;
    }}

    /* Sidebar */
    section[data-testid="stSidebar"] {{
        background: var(--sidebar) !important;
        border-right: 1px solid var(--card-bdr) !important;
    }}
    section[data-testid="stSidebar"] * {{
        color: var(--text) !important;
        
    }}
    section[data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"] > div,
    section[data-testid="stSidebar"] .stTextInput input,
    section[data-testid="stSidebar"] .stNumberInput input {{
        background: var(--card) !important;
        border-color: var(--card-bdr) !important;
        color: var(--text) !important;
        border-radius: 6px !important;
    }}
    section[data-testid="stSidebar"] .stButton > button {{
        background: var(--accent) !important;
        color: white !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 700 !important;
        font-size: 13px !important;
        letter-spacing: 0.5px !important;
        transition: all 0.2s !important;
    }}
    section[data-testid="stSidebar"] .stButton > button:hover {{
        opacity: 0.85 !important;
        transform: translateY(-1px) !important;
    }}

    /* Top nav */
    .top-nav {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 14px 24px;
        background: var(--bg2);
        border-bottom: 1px solid var(--card-bdr);
        border-radius: 12px;
        margin-bottom: 20px;
    }}
    .nav-brand {{
        display: flex;
        align-items: center;
        gap: 12px;
    }}
    .nav-logo {{
        width: 36px; height: 36px;
        background: linear-gradient(135deg, {T["ce"]}, {T["accent"]});
        border-radius: 8px;
        display: flex; align-items: center; justify-content: center;
        font-size: 18px;
    }}
    .nav-title {{
        font-family: "Syne", sans-serif !important;
        font-size: 18px;
        font-weight: 800;
        color: var(--text);
        letter-spacing: -0.3px;
    }}
    .nav-subtitle {{
        font-size: 11px;
        color: var(--text3);
        font-family: "Space Mono", monospace !important;
        margin-top: 1px;
    }}
    .nav-pills {{
        display: flex;
        gap: 8px;
        align-items: center;
    }}
    .nav-pill {{
        padding: 5px 12px;
        border-radius: 20px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }}
    .pill-live {{
        background: rgba(52, 211, 153, 0.15);
        color: {T["pe"]};
        border: 1px solid rgba(52, 211, 153, 0.3);
        animation: pulse 2s infinite;
    }}
    .pill-time {{
        background: var(--card);
        color: var(--text2);
        border: 1px solid var(--card-bdr);
        font-family: "Space Mono", monospace !important;
    }}
    @keyframes pulse {{
        0%, 100% {{ opacity: 1; }}
        50% {{ opacity: 0.5; }}
    }}

    /* Metric cards */
    .metrics-grid {{
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 12px;
        margin-bottom: 20px;
    }}
    @media (max-width: 768px) {{
        .metrics-grid {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    .metric-card {{
        background: var(--card);
        border: 1px solid var(--card-bdr);
        border-radius: 12px;
        padding: 18px 20px;
        position: relative;
        overflow: hidden;
        transition: transform 0.15s, box-shadow 0.15s;
    }}
    .metric-card:hover {{
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(0,0,0,0.3);
    }}
    .metric-card::before {{
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 3px;
        border-radius: 12px 12px 0 0;
    }}
    .card-ce::before   {{ background: {T["ce"]}; }}
    .card-pe::before   {{ background: {T["pe"]}; }}
    .card-diff::before {{ background: {T["diff"]}; }}
    .card-time::before {{ background: {T["accent"]}; }}

    .metric-label {{
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.5px;
        text-transform: uppercase;
        color: var(--text3);
        margin-bottom: 10px;
    }}
    .metric-value {{
        font-family: "Space Mono", monospace !important;
        font-size: 28px;
        font-weight: 700;
        line-height: 1;
        margin-bottom: 8px;
    }}
    .val-ce   {{ color: {T["ce"]}; }}
    .val-pe   {{ color: {T["pe"]}; }}
    .val-diff {{ color: {T["diff"]}; }}
    .val-time {{ color: {T["accent"]}; font-size: 22px; }}
    .metric-sub {{
        font-size: 10px;
        color: var(--text3);
        font-family: "Space Mono", monospace !important;
    }}
    .metric-badge {{
        position: absolute;
        top: 14px; right: 14px;
        font-size: 18px;
        opacity: 0.4;
    }}

    /* Sidebar sections */
    .sidebar-section {{
        background: var(--card);
        border: 1px solid var(--card-bdr);
        border-radius: 10px;
        padding: 14px;
        margin-bottom: 12px;
    }}
    .sidebar-title {{
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.5px;
        text-transform: uppercase;
        color: var(--text3);
        margin-bottom: 12px;
        display: flex;
        align-items: center;
        gap: 6px;
    }}

    /* Divider */
    hr {{ border-color: var(--divider) !important; }}

    /* Expander */
    .stExpander {{
        background: var(--card) !important;
        border: 1px solid var(--card-bdr) !important;
        border-radius: 10px !important;
    }}

    /* Streamlit overrides */
    .stMarkdown p {{ color: var(--text2); }}
    .stDataFrame {{ border-radius: 10px; overflow: hidden; }}
    div[data-testid="stDecoration"] {{ display: none; }}
    
    div[data-testid="stVerticalBlock"] > div {{ gap: 0rem !important; }}
    .stSelectbox, .stTextInput, .stNumberInput {{ margin-bottom: -18px !important; }}
    
    button[data-testid="collapsedControl"] {{ display: none !important; }}
    
    section[data-testid="stSidebar"] {{ min-width: 380px !important; max-width: 380px !important; }}
    div[data-baseweb="popover"] {{ left: 0 !important; right: auto !important; }}
    div[data-baseweb="calendar"] {{ left: 0 !important; right: auto !important; }}
    
    .block-container {{ padding-top: 0.5rem !important; }}
    header[data-testid="stHeader"] {{ background: transparent !important; }}
    header[data-testid="stHeader"] > * {{ display: none !important; }}
    header[data-testid="stHeader"] {{ height: 0 !important; min-height: 0 !important; }}
</style>
""", unsafe_allow_html=True)



# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# DATE LOGIC (shared)
# ─────────────────────────────────────────────

today = date.today()
if today.weekday() == 5:
    default_date = today - pd.Timedelta(days=1)
elif today.weekday() == 6:
    default_date = today - pd.Timedelta(days=2)
else:
    default_date = today



import datetime as _dt
_now = _dt.datetime.now().strftime("%H:%M:%S")


if st.button("🔄 Refresh Token", key="refresh_token_nav", help="Clear cached token and re-authenticate"):
    get_shared_token.clear()
    st.rerun()

st.markdown(f"""
<div class="top-nav">
    <div class="nav-brand">
        <div class="nav-logo">📊</div>
        <div>
            <div class="nav-title">NFO / BFO Spread Terminal</div>
            <div class="nav-subtitle">NFO / BFO Options Spread</div>
        </div>
    </div>
    <div class="nav-pills">
        <span class="nav-pill pill-live">● LIVE</span>
        <span class="nav-pill pill-time">{_now} IST</span>
    </div>
</div>
""", unsafe_allow_html=True)

st.markdown("""
<style>
div[data-testid="stMainBlockContainer"] > div > div > div:nth-child(1) button {
    position: fixed !important;
    top: 10px !important;
    right: 200px !important;
    z-index: 9999 !important;
    background: rgba(2,132,199,0.15) !important;
    border: 1px solid rgba(2,132,199,0.4) !important;
    color: #0284c7 !important;
    border-radius: 20px !important;
    padding: 3px 12px !important;
    font-size: 11px !important;
    font-weight: 600 !important;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# FETCH LIVE DATA
# ─────────────────────────────────────────────

def fetch_live_data():
    fyers = get_fyers_client()
    if fyers is None:
        return pd.DataFrame()

    with st.spinner("Fetching option & spot prices from Fyers..."):
        df_sx_ce   = fetch_candles(fyers, sym_sx_ce, candle_interval, date_str)
        df_sx_pe   = fetch_candles(fyers, sym_sx_pe, candle_interval, date_str)
        df_nf_ce   = fetch_candles(fyers, sym_nf_ce, candle_interval, date_str)
        df_nf_pe   = fetch_candles(fyers, sym_nf_pe, candle_interval, date_str)
        df_sx_spot = fetch_candles(fyers, "BSE:SENSEX-INDEX", candle_interval, date_str)
        if df_sx_spot.empty:
            df_sx_spot = fetch_candles(fyers, "BSE:SENSEX", candle_interval, date_str)
        df_nf_spot = fetch_candles(fyers, "NSE:NIFTY50-INDEX", candle_interval, date_str)
        if df_nf_spot.empty:
            df_nf_spot = fetch_candles(fyers, "NSE:NIFTY50", candle_interval, date_str)
        if df_sx_spot.empty or df_nf_spot.empty:
            st.session_state["spot_debug"] = f"SENSEX spot={len(df_sx_spot)} rows | NIFTY spot={len(df_nf_spot)} rows"
        else:
            st.session_state["spot_debug"] = ""

    if any(df.empty for df in [df_sx_ce, df_sx_pe, df_nf_ce, df_nf_pe]):
        st.warning("⚠️ One or more symbols returned no data. Check expiry/strike values in sidebar.")
        return pd.DataFrame()

    for df_ in [df_sx_ce, df_sx_pe, df_nf_ce, df_nf_pe, df_sx_spot, df_nf_spot]:
        df_ = df_[~df_.index.duplicated(keep="last")]

    df = pd.DataFrame({
        "sensex_ce": df_sx_ce["close"],
        "sensex_pe": df_sx_pe["close"],
        "nifty_ce" : df_nf_ce["close"],
        "nifty_pe" : df_nf_pe["close"],
    }).dropna()

    # Synthetic Future = Spot + CE - PE
    if not df_sx_spot.empty and not df_nf_spot.empty:
        df["sensex_spot"] = df_sx_spot["close"].reindex(df.index, method="ffill")
        df["nifty_spot"]  = df_nf_spot["close"].reindex(df.index, method="ffill")
        df["synth_sensex"] = df["sensex_spot"] + df["sensex_ce"] - df["sensex_pe"]
        df["synth_nifty"]  = df["nifty_spot"]  + df["nifty_ce"]  - df["nifty_pe"]
        df["synth_ratio"]  = df["synth_sensex"] / df["synth_nifty"]

    df["ce_spread"] = df["sensex_ce"] - (df["nifty_ce"] * multiplier)
    df["pe_spread"] = df["sensex_pe"] - (df["nifty_pe"] * multiplier)
    df["diff"]      = df["ce_spread"] + df["pe_spread"]
    return df

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────

if "df" not in st.session_state:
    st.session_state.df = pd.DataFrame()
if "df_custom" not in st.session_state:
    st.session_state.df_custom = pd.DataFrame()

# ─────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs(["📊 Spread Dashboard", "🧮 Butterfly", "📐 IV Analysis"])

# ─────────────────────────────────────────────
# TAB 2 — CUSTOM 4-LEG BUILDER
# ─────────────────────────────────────────────

with tab2:
    st.markdown("<div style='font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#64748b;margin-bottom:8px;'>Configure 4 Legs</div>", unsafe_allow_html=True)

    UNDERLYINGS = ["SENSEX", "BANKEX", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
    leg_colors  = ["#f87171", "#34d399", "#60a5fa", "#fbbf24"]
    leg_labels  = ["Leg 1", "Leg 2", "Leg 3", "Leg 4"]
    leg_configs = []

    # ── Next Tuesday (NSE) and next Thursday (BSE) expiry ──
    def next_weekday_str(weekday):
        d = date.today()
        days_ahead = weekday - d.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return (d + pd.Timedelta(days=days_ahead)).strftime("%y%m%d")

    nse_exp = next_weekday_str(1)  # Tuesday
    bse_exp = next_weekday_str(3)  # Thursday

    LEG_DEFAULTS = [
        ("NSE", "NIFTY",  nse_exp, 24200, "CE", 1.0),
        ("BSE", "SENSEX", bse_exp, 80000, "CE", 3.3),
        ("BSE", "SENSEX", bse_exp, 80000, "CE", 3.3),
        ("NSE", "NIFTY",  nse_exp, 24200, "CE", 1.0),
    ]

    if "c_defaults_set" not in st.session_state:
        for i, (ex, un, ep, st_, ot, mu) in enumerate(LEG_DEFAULTS):
            st.session_state[f"c_exch_{i}"]  = ex
            st.session_state[f"c_under_{i}"] = un
            st.session_state[f"c_exp_{i}"]   = ep
            st.session_state[f"c_str_{i}"]   = float(st_)
            st.session_state[f"c_opt_{i}"]   = ot
            st.session_state[f"c_lots_{i}"]  = mu
        st.session_state["c_defaults_set"] = True

    c_row = st.columns([1.2, 1, 1, 1, 1.5])
    with c_row[0]: custom_date     = st.date_input("Date",       value=default_date,           key="c_date")
    with c_row[1]: custom_interval = st.selectbox("Interval",    [1,3,5,10,15,30,60], index=2, key="c_interval")
    with c_row[2]: c_auto          = st.checkbox("Auto Refresh", value=False,                  key="c_auto_refresh")
    with c_row[3]: c_secs          = st.slider("Sec", 5, 60, 10,                               key="c_refresh_secs")
    with c_row[4]: custom_fetch    = st.button("⟳  FETCH 4-LEG DATA", type="primary", use_container_width=True, key="c_fetch")

    for i in range(4):
        d_exch, d_under, d_exp, d_str, d_opt, d_mult = LEG_DEFAULTS[i]
        st.markdown(f"<div style='font-size:11px;font-weight:700;color:{leg_colors[i]};margin:2px 0 0px 0;'>▸ {leg_labels[i]}</div>", unsafe_allow_html=True)
        cols = st.columns([1, 1.4, 1, 1, 1.2, 0.9])
        exch_opts = ["NSE","BSE"] if d_exch == "NSE" else ["BSE","NSE"]
        und_opts  = [d_under] + [u for u in UNDERLYINGS if u != d_under]
        with cols[0]: exch     = st.selectbox("Exchange",   exch_opts,            key=f"c_exch_{i}")
        with cols[1]: under    = st.selectbox("Underlying", und_opts,             key=f"c_under_{i}")
        with cols[2]: expiry   = st.text_input("Expiry",                          key=f"c_exp_{i}", help="YYMMDD or 26MAR")
        with cols[3]: strike   = st.number_input("Strike",  step=100,             key=f"c_str_{i}")
        with cols[4]: opt_type = st.selectbox("CE / PE",   ["CE","PE"],           key=f"c_opt_{i}")
        with cols[5]: mult     = st.number_input("Multiplier", min_value=0.1, step=0.1, key=f"c_lots_{i}")
        leg_configs.append({"exchange": exch, "underlying": under, "expiry": expiry,
                            "strike": int(strike), "opt_type": opt_type, "lots": mult})

    

    custom_date_str = custom_date.strftime("%Y-%m-%d")

    L = leg_configs
    def leg_name(i): return f"{L[i]['lots']}×{L[i]['underlying']} {L[i]['opt_type']}"
    st.markdown(f"""
    <div style='font-size:11px;color:#64748b;margin:4px 0 8px 0;font-family:monospace;'>
        Chart 1: &nbsp;<span style='color:#f87171'>{leg_name(0)}</span> − <span style='color:#34d399'>{leg_name(1)}</span>
        &nbsp;&nbsp;|&nbsp;&nbsp;
        <span style='color:#60a5fa'>{leg_name(2)}</span> − <span style='color:#fbbf24'>{leg_name(3)}</span>
        &nbsp;&nbsp;&nbsp;&nbsp;
        Chart 2: &nbsp;(Leg1−Leg2) + (Leg3−Leg4)
    </div>
    """, unsafe_allow_html=True)

    if custom_fetch:
        fyers = get_fyers_client()
        if fyers is None:
            st.error("Not connected to Fyers.")
        else:
            raw_series = []
            ok = True
            with st.spinner("Fetching 4-leg data..."):
                for i, leg in enumerate(leg_configs):
                    sym = build_symbol(leg["exchange"], leg["underlying"], leg["expiry"], leg["opt_type"][0], leg["strike"])
                    df_leg = fetch_candles(fyers, sym, custom_interval, custom_date_str)
                    if df_leg.empty:
                        st.warning(f"⚠️ {leg_labels[i]}: No data for `{sym}`")
                        ok = False
                        break
                    df_leg = df_leg[~df_leg.index.duplicated(keep="last")]
                    raw_series.append(df_leg["close"] * leg["lots"])

            if ok and len(raw_series) == 4:
                base_idx = raw_series[0].index
                s = [sr.reindex(base_idx, method="ffill").fillna(0) for sr in raw_series]
                spread12 = s[0] - s[1]
                spread34 = s[2] - s[3]
                combined = spread12 + spread34
                st.session_state.df_custom = pd.DataFrame({
                    "spread12": spread12,
                    "spread34": spread34,
                    "combined": combined,
                })

    df_custom = st.session_state.df_custom

    if not df_custom.empty:
        _s12  = df_custom['spread12'].iloc[-1]
        _s34  = df_custom['spread34'].iloc[-1]
        _comb = df_custom['combined'].iloc[-1]
        _s12_d  = _s12  - df_custom['spread12'].iloc[-2] if len(df_custom) > 1 else 0
        _s34_d  = _s34  - df_custom['spread34'].iloc[-2] if len(df_custom) > 1 else 0
        _comb_d = _comb - df_custom['combined'].iloc[-2] if len(df_custom) > 1 else 0
        _updated = df_custom.index[-1].strftime("%H:%M:%S")

        def _darrow(v):
            arrow = "▲" if v >= 0 else "▼"
            color = "#f87171" if v >= 0 else "#34d399"
            return f"<span style='color:{color};font-size:11px;'>{arrow} {abs(v):.2f}</span>"

        st.markdown(f"""
        <div class="metrics-grid">
            <div class="metric-card card-ce">
                <div class="metric-badge">📊</div>
                <div class="metric-label">LEG 1 − LEG 2</div>
                <div class="metric-value val-ce">{_s12:+.1f}</div>
                <div class="metric-sub">Spread &nbsp; {_darrow(_s12_d)}</div>
            </div>
            <div class="metric-card card-pe">
                <div class="metric-badge">📊</div>
                <div class="metric-label">LEG 3 − LEG 4</div>
                <div class="metric-value val-pe">{_s34:+.1f}</div>
                <div class="metric-sub">Spread &nbsp; {_darrow(_s34_d)}</div>
            </div>
            <div class="metric-card card-diff">
                <div class="metric-badge">⚖️</div>
                <div class="metric-label">4 LEG TOTAL</div>
                <div class="metric-value val-diff">{_comb:+.1f}</div>
                <div class="metric-sub">(Leg1−Leg2) + (Leg3−Leg4) &nbsp; {_darrow(_comb_d)}</div>
            </div>
            <div class="metric-card card-time">
                <div class="metric-badge">🕐</div>
                <div class="metric-label">LAST UPDATE</div>
                <div class="metric-value val-time">{_updated}</div>
                <div class="metric-sub">{len(df_custom)} candles</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        def make_hlines(fig, series, colors):
            h, l = series.max(), series.min()
            fig.add_hline(y=0, line_dash="dash", line_color="#444")
            fig.add_hline(y=h, line_dash="dot", line_color=colors[0], line_width=1,
                annotation_text=f"H: {h:.0f}", annotation_position="right",
                annotation_font=dict(color=colors[0], size=10))
            fig.add_hline(y=l, line_dash="dot", line_color=colors[1], line_width=1,
                annotation_text=f"L: {l:.0f}", annotation_position="right",
                annotation_font=dict(color=colors[1], size=10))

        def chart_layout(fig, title, height=380):
            fig.update_layout(
                title=dict(text=title, font=dict(size=12, color=T["text2"]), x=0),
                height=height,
                plot_bgcolor=T["plot_bg"], paper_bgcolor=T["plot_bg"],
                font=dict(color=T["text2"]),
                hovermode="x unified",
                margin=dict(l=10, r=10, t=40, b=10),
                legend=dict(bgcolor=T["card"], bordercolor=T["card_bdr"], borderwidth=1,
                    orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                xaxis=dict(gridcolor=T["grid"], tickfont=dict(size=10),
                    showspikes=True, spikemode="across", spikecolor=T["text3"], spikethickness=1, spikedash="dot"),
                yaxis=dict(gridcolor=T["grid"], title="Value (₹)", tickfont=dict(size=10),
                    showspikes=True, spikemode="across", spikecolor=T["text3"], spikethickness=1, spikedash="dot"),
                hoverlabel=dict(bgcolor=T["card"], bordercolor=T["card_bdr"], font=dict(color=T["text"])),
            )

        fig1 = go.Figure()
        fig1.add_trace(go.Scatter(x=df_custom.index, y=df_custom["spread12"],
            name="Leg1 − Leg2", line=dict(color="#f87171", width=2),
            hovertemplate="%{x|%H:%M}<br>Leg1−Leg2: %{y:.2f}<extra></extra>"))
        fig1.add_trace(go.Scatter(x=df_custom.index, y=df_custom["spread34"],
            name="Leg3 − Leg4", line=dict(color="#60a5fa", width=2),
            hovertemplate="%{x|%H:%M}<br>Leg3−Leg4: %{y:.2f}<extra></extra>"))
        make_hlines(fig1, df_custom["spread12"], ["#f87171", "#f87171"])
        chart_layout(fig1, "Spread Chart — Leg1−Leg2  &  Leg3−Leg4")
        st.plotly_chart(fig1, use_container_width=True)

        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=df_custom.index, y=df_custom["combined"],
            name="Combined (Leg1−Leg2) + (Leg3−Leg4)",
            line=dict(color="#818cf8", width=2.5),
            fill="tozeroy", fillcolor="rgba(129,140,248,0.08)",
            hovertemplate="%{x|%H:%M}<br>Combined: %{y:.2f}<extra></extra>"))
        make_hlines(fig2, df_custom["combined"], ["#34d399", "#f87171"])
        chart_layout(fig2, "Combined Chart — (Leg1−Leg2) + (Leg3−Leg4)")
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("👆 Configure your 4 legs above and click **Fetch 4-Leg Data**.")

# ─────────────────────────────────────────────
# TAB 1 — SPREAD DASHBOARD
# ─────────────────────────────────────────────

with tab1:
    # ── Controls ──
    st.markdown("<div style='font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#64748b;margin-bottom:4px;'>⚙ Settings</div>", unsafe_allow_html=True)
    r0 = st.columns([1.2, 1, 1, 1, 1, 1, 1, 1.5])
    with r0[0]: selected_date  = st.date_input("Date", value=default_date, key="date_inp")
    with r0[1]: multiplier     = st.number_input("Multiplier", value=3.3, step=0.1, min_value=0.1, key="mult")
    with r0[2]: candle_interval= st.selectbox("Interval (min)", [1, 3, 5, 10, 15, 30, 60], index=2, key="interval")
    with r0[3]: show_diff      = st.checkbox("4-Leg Chart", value=True, key="show_diff")
    with r0[4]: auto_refresh   = st.checkbox("Auto Refresh", value=True, key="auto_ref")
    with r0[5]: refresh_secs   = st.slider("Refresh (sec)", 5, 60, REFRESH_SECONDS, key="ref_sec")
    with r0[6]: st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    with r0[7]: fetch_btn      = st.button("⟳  FETCH DATA", use_container_width=True, type="primary", key="fetch_btn")

    date_str = selected_date.strftime("%Y-%m-%d")

    st.markdown("<div style='margin:6px 0 4px 0;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#64748b;'>▸ Leg 1 — Base</div>", unsafe_allow_html=True)
    r1 = st.columns([1, 1.5, 1.2, 1.2, 1.2, 1.2])
    with r1[0]: sensex_exchange   = st.selectbox("Exchange", ["BSE", "NSE"], index=0, key="sx_exch")
    with r1[1]: sensex_underlying = st.selectbox("Underlying", ["SENSEX", "BANKEX", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"], index=0, key="sx_under")
    with r1[2]: sensex_ce_expiry  = st.text_input("CE Expiry", value="260312", key="sx_ce_exp", help="YYMMDD")
    with r1[3]: sensex_pe_expiry  = st.text_input("PE Expiry", value="260312", key="sx_pe_exp")
    with r1[4]: sensex_ce_strike  = st.number_input("CE Strike", value=80000, step=100, key="sx_ce_str")
    with r1[5]: sensex_pe_strike  = st.number_input("PE Strike", value=80000, step=100, key="sx_pe_str")

    st.markdown("<div style='margin:6px 0 4px 0;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#64748b;'>▸ Leg 2 — Hedge</div>", unsafe_allow_html=True)
    r2 = st.columns([1, 1.5, 1.2, 1.2, 1.2, 1.2])
    with r2[0]: nifty_exchange   = st.selectbox("Exchange", ["NSE", "BSE"], index=0, key="nf_exch")
    with r2[1]: nifty_underlying = st.selectbox("Underlying", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"], index=0, key="nf_under")
    with r2[2]: nifty_ce_expiry  = st.text_input("CE Expiry", value="260310", key="nf_ce_exp", help="YYMMDD")
    with r2[3]: nifty_pe_expiry  = st.text_input("PE Expiry", value="260310", key="nf_pe_exp")
    with r2[4]: nifty_ce_strike  = st.number_input("CE Strike", value=24800, step=50, key="nf_ce_str")
    with r2[5]: nifty_pe_strike  = st.number_input("PE Strike", value=24800, step=50, key="nf_pe_str")

    st.divider()

    # ── Symbols ──
    sym_sx_ce = build_symbol(sensex_exchange, sensex_underlying, sensex_ce_expiry, "C", int(sensex_ce_strike))
    sym_sx_pe = build_symbol(sensex_exchange, sensex_underlying, sensex_pe_expiry, "P", int(sensex_pe_strike))
    sym_nf_ce = build_symbol(nifty_exchange,  nifty_underlying,  nifty_ce_expiry,  "C", int(nifty_ce_strike))
    sym_nf_pe = build_symbol(nifty_exchange,  nifty_underlying,  nifty_pe_expiry,  "P", int(nifty_pe_strike))

    if fetch_btn or st.session_state.df.empty:
        st.session_state.df = fetch_live_data()
    df = st.session_state.df

    if df.empty:
        st.info("👆 Set your options above and click **Fetch Data**.")
    else:
        latest   = df.iloc[-1]
        ce_val   = latest["ce_spread"]
        pe_val   = latest["pe_spread"]
        diff_val = latest["diff"]
        updated  = df.index[-1].strftime("%H:%M:%S")
        is_today = date_str == date.today().strftime("%Y-%m-%d")

        ce_delta  = ce_val  - df["ce_spread"].iloc[-2]  if len(df) > 1 else 0
        pe_delta  = pe_val  - df["pe_spread"].iloc[-2]  if len(df) > 1 else 0
        diff_delta= diff_val- df["diff"].iloc[-2]        if len(df) > 1 else 0

        def delta_html(v):
            arrow = "▲" if v >= 0 else "▼"
            color = "#f87171" if v >= 0 else "#34d399"
            return f"<span style='color:{color};font-size:11px;font-family:Space Mono'>{arrow} {abs(v):.2f}</span>"

        st.markdown(f"""
        <div class="metrics-grid">
            <div class="metric-card card-ce">
                <div class="metric-badge">📈</div>
                <div class="metric-label">CE SPREAD</div>
                <div class="metric-value val-ce">{ce_val:+.1f}</div>
                <div class="metric-sub">{sensex_underlying} CE − {nifty_underlying} CE &times;{multiplier} &nbsp; {delta_html(ce_delta)}</div>
            </div>
            <div class="metric-card card-pe">
                <div class="metric-badge">📉</div>
                <div class="metric-label">PE SPREAD</div>
                <div class="metric-value val-pe">{pe_val:+.1f}</div>
                <div class="metric-sub">{sensex_underlying} PE − {nifty_underlying} PE &times;{multiplier} &nbsp; {delta_html(pe_delta)}</div>
            </div>
            <div class="metric-card card-diff">
                <div class="metric-badge">⚖️</div>
                <div class="metric-label">4 LEG TOTAL</div>
                <div class="metric-value val-diff">{diff_val:+.1f}</div>
                <div class="metric-sub">CE + PE combined &nbsp; {delta_html(diff_delta)}</div>
            </div>
            <div class="metric-card card-time">
                <div class="metric-badge">{'🔴' if is_today else '📂'}</div>
                <div class="metric-label">LAST UPDATE</div>
                <div class="metric-value val-time">{updated}</div>
                <div class="metric-sub">{'LIVE' if is_today else 'HISTORICAL'} &nbsp;·&nbsp; {len(df)} candles</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        has_synth = "synth_ratio" in df.columns and df["synth_ratio"].notna().any()

        # Determine rows
        n_rows = 1 + int(show_diff) + int(has_synth)
        if n_rows == 3:
            row_heights = [0.55, 0.25, 0.20]
        elif n_rows == 2:
            row_heights = [0.70, 0.30]
        else:
            row_heights = [1.0]

        fig = make_subplots(
            rows=n_rows, cols=1,
            shared_xaxes=True,
            row_heights=row_heights,
            vertical_spacing=0.04
        )

        diff_row  = 2 if show_diff else None
        synth_row = (3 if show_diff else 2) if has_synth else None

        fig.add_trace(go.Scatter(x=df.index, y=df["ce_spread"], name="CE Spread", line=dict(color="#ff4444", width=2), hovertemplate="%{x|%H:%M}<br>CE: %{y:.2f}<extra></extra>"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=df["pe_spread"], name="PE Spread", line=dict(color="#44ff88", width=2), hovertemplate="%{x|%H:%M}<br>PE: %{y:.2f}<extra></extra>"), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=pd.concat([df.index.to_series(), df.index.to_series()[::-1]]).values,
            y=pd.concat([df["ce_spread"], df["pe_spread"][::-1]]).values,
            fill="toself", fillcolor="rgba(255,100,100,0.07)",
            line=dict(color="rgba(0,0,0,0)"), showlegend=False, hoverinfo="skip"
        ), row=1, col=1)
        fig.add_hline(y=0, line_dash="dash", line_color="#444", row=1, col=1)

        if show_diff:
            fig.add_trace(go.Scatter(x=df.index, y=df["diff"], name="4 Leg", line=dict(color="#ffaa00", width=2), hovertemplate="%{x|%H:%M}<br>4 Leg: %{y:.2f}<extra></extra>"), row=diff_row, col=1)
            fig.add_hline(y=0, line_dash="dash", line_color="#444", row=diff_row, col=1)
            diff_high = df["diff"].max()
            diff_low  = df["diff"].min()
            fig.add_hline(y=diff_high, line_dash="dot", line_color="#ffaa00", line_width=1, opacity=0.5,
                annotation_text=f"H: {diff_high:.0f}", annotation_position="right",
                annotation_font=dict(color="#ffaa00", size=10), row=diff_row, col=1)
            fig.add_hline(y=diff_low, line_dash="dot", line_color="#ffaa00", line_width=1, opacity=0.5,
                annotation_text=f"L: {diff_low:.0f}", annotation_position="right",
                annotation_font=dict(color="#ffaa00", size=10), row=diff_row, col=1)

        if has_synth:
            fig.add_trace(go.Scatter(
                x=df.index, y=df["synth_ratio"], name="Synth Ratio",
                line=dict(color="#818cf8", width=2),
                hovertemplate="%{x|%H:%M}<br>Synth: %{y:.4f}<extra></extra>"
            ), row=synth_row, col=1)
            ratio_high = df["synth_ratio"].max()
            ratio_low  = df["synth_ratio"].min()
            fig.add_hline(y=ratio_high, line_dash="dot", line_color="#818cf8", line_width=1, opacity=0.5,
                annotation_text=f"H: {ratio_high:.4f}", annotation_position="right",
                annotation_font=dict(color="#818cf8", size=10), row=synth_row, col=1)
            fig.add_hline(y=ratio_low, line_dash="dot", line_color="#818cf8", line_width=1, opacity=0.5,
                annotation_text=f"L: {ratio_low:.4f}", annotation_position="right",
                annotation_font=dict(color="#818cf8", size=10), row=synth_row, col=1)

        fig.update_layout(
            height=580 + (120 if has_synth else 0),
            plot_bgcolor=T["plot_bg"],
            paper_bgcolor=T["plot_bg"],
            font=dict(color=T["text2"], family="Space Mono"),
            legend=dict(
                bgcolor=T["card"], bordercolor=T["card_bdr"],
                borderwidth=1, font=dict(size=11),
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
            hovermode="x unified",
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis=dict(gridcolor=T["grid"], tickfont=dict(size=10)),
            yaxis=dict(gridcolor=T["grid"], title="Spread (₹)", tickfont=dict(size=10)),
            hoverlabel=dict(bgcolor=T["card"], bordercolor=T["card_bdr"], font=dict(color=T["text"])),
        )
        if show_diff:
            fig.update_yaxes(gridcolor=T["grid"], title_text="4 Leg", tickfont=dict(size=10), row=diff_row, col=1)
            fig.update_xaxes(gridcolor=T["grid"], tickfont=dict(size=10), row=diff_row, col=1)
        if has_synth:
            fig.update_yaxes(gridcolor=T["grid"], title_text="Synth Ratio", tickfont=dict(size=10), row=synth_row, col=1)
            fig.update_xaxes(gridcolor=T["grid"], tickfont=dict(size=10), row=synth_row, col=1)

        st.plotly_chart(fig, use_container_width=True)
        if st.session_state.get("spot_debug"):
            st.warning(f"⚠️ Spot debug: {st.session_state['spot_debug']}")



    # ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# TAB 3 — IV ANALYSIS
# ─────────────────────────────────────────────

with tab3:
    SENSEX_LOT = 20
    NIFTY_LOT  = 65

    def bs_price(S, K, T, r, sigma, opt):
        import math
        if T <= 0 or sigma <= 0:
            return max(0.0, S - K) if opt == "CE" else max(0.0, K - S)
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        from scipy.stats import norm
        if opt == "CE":
            return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
        else:
            return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    def calc_iv(mkt_price, S, K, T, r, opt):
        from scipy.optimize import brentq
        if T <= 0 or S <= 0 or K <= 0 or mkt_price <= 0:
            return float("nan")
        try:
            return brentq(lambda sig: bs_price(S, K, T, r, sig, opt) - mkt_price,
                          1e-6, 20.0, xtol=1e-6, maxiter=200) * 100
        except Exception:
            return float("nan")

    def expiry_to_date(s):
        s = s.strip()
        return date(2000 + int(s[0:2]), int(s[2:4]), int(s[4:6]))

    def round_to(val, base):
        return int(base * round(float(val) / base))

    # ── Controls ──
    st.markdown("<div style='font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#64748b;margin-bottom:4px;'>⚙ IV Settings</div>", unsafe_allow_html=True)
    iv_r0 = st.columns([1.2, 1, 1, 1, 1, 1.5])
    with iv_r0[0]: iv_date      = st.date_input("Date",          value=default_date,           key="iv_date")
    with iv_r0[1]: iv_interval  = st.selectbox("Interval (min)", [1,3,5,10,15,30,60], index=2, key="iv_interval")
    with iv_r0[2]: sx_iv_expiry = st.text_input("Sensex Expiry", value="260312",               key="iv_sx_exp", help="YYMMDD")
    with iv_r0[3]: nf_iv_expiry = st.text_input("Nifty Expiry",  value="260310",               key="iv_nf_exp", help="YYMMDD")
    with iv_r0[4]: iv_rate      = st.number_input("Risk-Free %", value=6.5, step=0.1,          key="iv_rate")
    with iv_r0[5]: iv_fetch     = st.button("⟳  FETCH IV DATA", type="primary", use_container_width=True, key="iv_fetch")

    iv_date_str = iv_date.strftime("%Y-%m-%d")
    iv_mult     = st.session_state.get("mult", 3.3)

    st.markdown(f"<div style='font-size:11px;color:#64748b;margin:2px 0 8px 0;'>Using multiplier: <b>{iv_mult}</b> from Spread Dashboard &nbsp;|&nbsp; Sensex lot: <b>{SENSEX_LOT}</b> &nbsp;|&nbsp; Nifty lot: <b>{NIFTY_LOT}</b></div>", unsafe_allow_html=True)

    if iv_fetch:
        fyers = get_fyers_client()
        if fyers is None:
            st.error("Not connected to Fyers.")
        else:
            with st.spinner("Fetching spot & option data for IV..."):
                df_sx_s = fetch_candles(fyers, "BSE:SENSEX-INDEX",  iv_interval, iv_date_str)
                if df_sx_s.empty:
                    df_sx_s = fetch_candles(fyers, "BSE:SENSEX", iv_interval, iv_date_str)
                df_nf_s = fetch_candles(fyers, "NSE:NIFTY50-INDEX", iv_interval, iv_date_str)
                if df_nf_s.empty:
                    df_nf_s = fetch_candles(fyers, "NSE:NIFTY50", iv_interval, iv_date_str)

            if df_sx_s.empty or df_nf_s.empty:
                st.error("⚠️ Could not fetch spot prices.")
            else:
                df_sx_s = df_sx_s[~df_sx_s.index.duplicated(keep="last")]
                df_nf_s = df_nf_s[~df_nf_s.index.duplicated(keep="last")]

                last_sx = df_sx_s["close"].iloc[-1]

                # ── Dynamic ATM: compute per-minute strikes ──
                # Graph 1 = floor(spot/500)*500   Graph 2 = floor+500
                common_ts = df_sx_s.index.intersection(df_nf_s.index)
                sx_prices = df_sx_s["close"].reindex(common_ts)

                def atm_lo(s): 
                    k = int(s // 500) * 500
                    return k
                def atm_hi(s): return atm_lo(s) + 500

                lo_sx_series = sx_prices.apply(atm_lo)   # per-minute lower strike
                hi_sx_series = sx_prices.apply(atm_hi)   # per-minute upper strike
                lo_nf_series = lo_sx_series.apply(lambda k: round_to(k / iv_mult, 50))
                hi_nf_series = hi_sx_series.apply(lambda k: round_to(k / iv_mult, 50))

                # All unique strikes needed
                uniq_sx_lo = sorted(lo_sx_series.unique())
                uniq_sx_hi = sorted(hi_sx_series.unique())
                uniq_nf_lo = sorted(lo_nf_series.unique())
                uniq_nf_hi = sorted(hi_nf_series.unique())

                # Fetch all unique option series
                all_syms = {}
                for k in uniq_sx_lo:
                    all_syms[f"sx_lo_CE_{k}"] = build_symbol("BSE","SENSEX", sx_iv_expiry,"C", int(k))
                    all_syms[f"sx_lo_PE_{k}"] = build_symbol("BSE","SENSEX", sx_iv_expiry,"P", int(k))
                for k in uniq_sx_hi:
                    all_syms[f"sx_hi_CE_{k}"] = build_symbol("BSE","SENSEX", sx_iv_expiry,"C", int(k))
                    all_syms[f"sx_hi_PE_{k}"] = build_symbol("BSE","SENSEX", sx_iv_expiry,"P", int(k))
                for k in uniq_nf_lo:
                    all_syms[f"nf_lo_CE_{k}"] = build_symbol("NSE","NIFTY",  nf_iv_expiry,"C", int(k))
                    all_syms[f"nf_lo_PE_{k}"] = build_symbol("NSE","NIFTY",  nf_iv_expiry,"P", int(k))
                for k in uniq_nf_hi:
                    all_syms[f"nf_hi_CE_{k}"] = build_symbol("NSE","NIFTY",  nf_iv_expiry,"C", int(k))
                    all_syms[f"nf_hi_PE_{k}"] = build_symbol("NSE","NIFTY",  nf_iv_expiry,"P", int(k))

                with st.spinner(f"Fetching {len(all_syms)} option series (dynamic ATM)..."):
                    fetched = {}
                    for key, sym in all_syms.items():
                        df_o = fetch_candles(fyers, sym, iv_interval, iv_date_str)
                        fetched[key] = df_o[~df_o.index.duplicated(keep="last")] if not df_o.empty else df_o

                # ── Compute IV per minute using that minute's ATM strike ──
                r = iv_rate / 100
                exp_sx = expiry_to_date(sx_iv_expiry)
                exp_nf = expiry_to_date(nf_iv_expiry)

                def dynamic_iv_series(spot_df, strike_series, fetched_dict, prefix, expiry_dt, opt_type, nf_strike_series=None):
                    """Build IV series where strike can change each minute."""
                    out_iv    = {}
                    out_strike = {}
                    for ts in common_ts:
                        if ts not in spot_df.index: continue
                        S  = spot_df.loc[ts, "close"]
                        K  = int(strike_series.loc[ts]) if nf_strike_series is None else int(nf_strike_series.loc[ts])
                        opt_key = f"{prefix}_{K}"
                        df_o = fetched_dict.get(opt_key)
                        if df_o is None or df_o.empty or ts not in df_o.index: continue
                        price = df_o.loc[ts, "close"]
                        T = max((expiry_dt - ts.date()).days / 365, 1/365)
                        out_iv[ts]     = calc_iv(price, S, K, T, r, opt_type)
                        out_strike[ts] = K
                    return pd.Series(out_iv, dtype=float), pd.Series(out_strike, dtype=float)

                iv_sx_lo_CE, sk_sx_lo_CE = dynamic_iv_series(df_sx_s, lo_sx_series, fetched, "sx_lo_CE", exp_sx, "CE")
                iv_sx_lo_PE, sk_sx_lo_PE = dynamic_iv_series(df_sx_s, lo_sx_series, fetched, "sx_lo_PE", exp_sx, "PE")
                iv_nf_lo_CE, sk_nf_lo_CE = dynamic_iv_series(df_nf_s, lo_nf_series, fetched, "nf_lo_CE", exp_nf, "CE")
                iv_nf_lo_PE, sk_nf_lo_PE = dynamic_iv_series(df_nf_s, lo_nf_series, fetched, "nf_lo_PE", exp_nf, "PE")
                iv_sx_hi_CE, sk_sx_hi_CE = dynamic_iv_series(df_sx_s, hi_sx_series, fetched, "sx_hi_CE", exp_sx, "CE")
                iv_sx_hi_PE, sk_sx_hi_PE = dynamic_iv_series(df_sx_s, hi_sx_series, fetched, "sx_hi_PE", exp_sx, "PE")
                iv_nf_hi_CE, sk_nf_hi_CE = dynamic_iv_series(df_nf_s, hi_nf_series, fetched, "nf_hi_CE", exp_nf, "CE")
                iv_nf_hi_PE, sk_nf_hi_PE = dynamic_iv_series(df_nf_s, hi_nf_series, fetched, "nf_hi_PE", exp_nf, "PE")

                # Strike-change timestamps (when ATM shifted)
                def strike_changes(sk_series):
                    """Return list of (timestamp, new_strike) where strike changed."""
                    changes = []
                    prev = None
                    for ts, k in sk_series.items():
                        if prev is not None and k != prev:
                            changes.append((ts, int(k)))
                        prev = k
                    return changes

                iv_res = {
                    "sx_lo_CE": iv_sx_lo_CE, "sx_lo_PE": iv_sx_lo_PE,
                    "nf_lo_CE": iv_nf_lo_CE, "nf_lo_PE": iv_nf_lo_PE,
                    "sx_hi_CE": iv_sx_hi_CE, "sx_hi_PE": iv_sx_hi_PE,
                    "nf_hi_CE": iv_nf_hi_CE, "nf_hi_PE": iv_nf_hi_PE,
                }
                iv_changes = {
                    "lo_sx": strike_changes(sk_sx_lo_CE),
                    "lo_nf": strike_changes(sk_nf_lo_CE),
                    "hi_sx": strike_changes(sk_sx_hi_CE),
                    "hi_nf": strike_changes(sk_nf_hi_CE),
                }

                st.session_state["iv_res"]     = iv_res
                st.session_state["iv_changes"] = iv_changes
                st.session_state["iv_lo_sx"]   = int(lo_sx_series.iloc[-1])
                st.session_state["iv_hi_sx"]   = int(hi_sx_series.iloc[-1])
                st.session_state["iv_lo_nf"]   = int(lo_nf_series.iloc[-1])
                st.session_state["iv_hi_nf"]   = int(hi_nf_series.iloc[-1])
                st.session_state["iv_last_sx"] = last_sx

    # ── Render ──
    if "iv_res" in st.session_state:
        iv_res    = st.session_state["iv_res"]
        iv_changes= st.session_state.get("iv_changes", {})
        lo_sx     = st.session_state["iv_lo_sx"]; hi_sx    = st.session_state["iv_hi_sx"]
        lo_nf     = st.session_state["iv_lo_nf"]; hi_nf    = st.session_state["iv_hi_nf"]
        last_sx   = st.session_state.get("iv_last_sx", 0)

        st.markdown(f"""<div style='font-size:12px;color:#64748b;background:#f1f5f9;
            border-radius:6px;padding:8px 12px;margin:4px 0 10px 0;font-family:monospace;'>
            Sensex spot: <b>{last_sx:.0f}</b> &nbsp;→&nbsp;
            Graph 1: Sensex <b>{lo_sx}</b> / Nifty <b>{lo_nf}</b>
            &nbsp;|&nbsp;
            Graph 2: Sensex <b>{hi_sx}</b> / Nifty <b>{hi_nf}</b>
        </div>""", unsafe_allow_html=True)

        COLORS = {"sx_CE":"#f87171","sx_PE":"#fca5a5","nf_CE":"#60a5fa","nf_PE":"#93c5fd"}

        def card(label, series, color, lot):
            if series is not None and not series.dropna().empty:
                val = series.dropna().iloc[-1]
                return f"""<div style='background:#fff;border:1px solid #e2e8f0;border-top:3px solid {color};
                    border-radius:8px;padding:10px 12px;'>
                    <div style='font-size:9px;font-weight:700;letter-spacing:1px;color:#94a3b8;text-transform:uppercase;'>{label}</div>
                    <div style='font-size:22px;font-weight:800;color:{color};margin:4px 0 2px 0;'>{val:.1f}%</div>
                    <div style='font-size:11px;color:#64748b;'>Lot: {lot}</div></div>"""
            return f"""<div style='background:#fff;border:1px solid #e2e8f0;border-top:3px solid {color};
                border-radius:8px;padding:10px 12px;'>
                <div style='font-size:9px;color:#94a3b8;text-transform:uppercase;'>{label}</div>
                <div style='font-size:16px;color:#cbd5e1;'>No data</div></div>"""

        def iv_chart(pfx, sx_k, nf_k, title, chg_sx=None, chg_nf=None):
            fig = go.Figure()
            entries = [
                (f"{pfx}_CE", f"Sensex {sx_k} CE", COLORS["sx_CE"], 2,   SENSEX_LOT),
                (f"{pfx}_PE", f"Sensex {sx_k} PE", COLORS["sx_PE"], 1.5, SENSEX_LOT),
            ]
            nfx = pfx.replace("sx","nf")
            entries += [
                (f"{nfx}_CE", f"Nifty {nf_k} CE",  COLORS["nf_CE"], 2,   NIFTY_LOT),
                (f"{nfx}_PE", f"Nifty {nf_k} PE",  COLORS["nf_PE"], 1.5, NIFTY_LOT),
            ]
            for key, name, color, width, lot in entries:
                s = iv_res.get(key, pd.Series(dtype=float)).dropna()
                if not s.empty:
                    fig.add_trace(go.Scatter(x=s.index, y=s.values, name=f"{name} ({s.iloc[-1]:.1f}%)",
                        line=dict(color=color, width=width),
                        hovertemplate="%{x|%H:%M}<br>IV: %{y:.2f}%<extra>" + name + "</extra>"))

            # Strike-change vertical lines — Sensex (orange) and Nifty (purple)
            for ts, new_k in (chg_sx or []):
                fig.add_vline(x=str(ts), line_dash="dash", line_color="#f97316", line_width=1.5,
                    annotation_text=f"SX→{new_k}", annotation_position="top",
                    annotation_font=dict(color="#f97316", size=9))
            for ts, new_k in (chg_nf or []):
                fig.add_vline(x=str(ts), line_dash="dot", line_color="#a855f7", line_width=1.5,
                    annotation_text=f"NF→{new_k}", annotation_position="bottom",
                    annotation_font=dict(color="#a855f7", size=9))

            fig.update_layout(
                title=dict(text=title, font=dict(size=12, color="#64748b"), x=0),
                height=400, plot_bgcolor="#f8fafc", paper_bgcolor="#f8fafc",
                font=dict(color="#334155"), hovermode="x unified",
                margin=dict(l=10, r=10, t=40, b=10),
                legend=dict(bgcolor="#fff", bordercolor="#e2e8f0", borderwidth=1,
                    orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                xaxis=dict(gridcolor="#e2e8f0", tickfont=dict(size=10),
                    showspikes=True, spikemode="across", spikecolor="#94a3b8", spikethickness=1),
                yaxis=dict(gridcolor="#e2e8f0", title="IV %", tickfont=dict(size=10),
                    showspikes=True, spikemode="across", spikecolor="#94a3b8", spikethickness=1),
                hoverlabel=dict(bgcolor="#fff", bordercolor="#e2e8f0"),
            )
            return fig

        # Graph 1
        st.markdown(f"<div style='font-size:12px;font-weight:700;color:#64748b;margin:10px 0 6px 0;'>📉 Graph 1 — Sensex {lo_sx} / Nifty {lo_nf}</div>", unsafe_allow_html=True)
        c1,c2,c3,c4 = st.columns(4)
        c1.markdown(card(f"Sensex {lo_sx} CE IV", iv_res.get("sx_lo_CE"), COLORS["sx_CE"], SENSEX_LOT), unsafe_allow_html=True)
        c2.markdown(card(f"Sensex {lo_sx} PE IV", iv_res.get("sx_lo_PE"), COLORS["sx_PE"], SENSEX_LOT), unsafe_allow_html=True)
        c3.markdown(card(f"Nifty {lo_nf} CE IV",  iv_res.get("nf_lo_CE"), COLORS["nf_CE"], NIFTY_LOT),  unsafe_allow_html=True)
        c4.markdown(card(f"Nifty {lo_nf} PE IV",  iv_res.get("nf_lo_PE"), COLORS["nf_PE"], NIFTY_LOT),  unsafe_allow_html=True)
        st.plotly_chart(iv_chart("sx_lo", lo_sx, lo_nf, f"IV % — Sensex {lo_sx} & Nifty {lo_nf} (dynamic ATM)", iv_changes.get("lo_sx"), iv_changes.get("lo_nf")), use_container_width=True)

        # Graph 2
        st.markdown(f"<div style='font-size:12px;font-weight:700;color:#64748b;margin:10px 0 6px 0;'>📈 Graph 2 — Sensex {hi_sx} / Nifty {hi_nf}</div>", unsafe_allow_html=True)
        c1,c2,c3,c4 = st.columns(4)
        c1.markdown(card(f"Sensex {hi_sx} CE IV", iv_res.get("sx_hi_CE"), COLORS["sx_CE"], SENSEX_LOT), unsafe_allow_html=True)
        c2.markdown(card(f"Sensex {hi_sx} PE IV", iv_res.get("sx_hi_PE"), COLORS["sx_PE"], SENSEX_LOT), unsafe_allow_html=True)
        c3.markdown(card(f"Nifty {hi_nf} CE IV",  iv_res.get("nf_hi_CE"), COLORS["nf_CE"], NIFTY_LOT),  unsafe_allow_html=True)
        c4.markdown(card(f"Nifty {hi_nf} PE IV",  iv_res.get("nf_hi_PE"), COLORS["nf_PE"], NIFTY_LOT),  unsafe_allow_html=True)
        st.plotly_chart(iv_chart("sx_hi", hi_sx, hi_nf, f"IV % — Sensex {hi_sx} & Nifty {hi_nf} (dynamic ATM)", iv_changes.get("hi_sx"), iv_changes.get("hi_nf")), use_container_width=True)

    else:
        st.info("👆 Set expiry dates above and click **Fetch IV Data**.")

# AUTO REFRESH
# ─────────────────────────────────────────────

if auto_refresh and date_str == date.today().strftime("%Y-%m-%d") and not df.empty:
    time.sleep(refresh_secs)
    st.session_state.df = fetch_live_data()
    st.rerun()
