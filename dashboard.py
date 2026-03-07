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

def get_fyers_client():
    # Return cached client using stored token string — survives reruns
    if "access_token" in st.session_state and st.session_state.access_token:
        cid = get_secret("FYERS_CLIENT_ID") or CLIENT_ID
        return fyersModel.FyersModel(client_id=cid, token=st.session_state.access_token, log_path="")

    # Try local token file first (local PC)
    try:
        client = load_fyers_from_file()
        with open(TOKEN_FILE) as f:
            st.session_state.access_token = f.read().strip()
        return client
    except FileNotFoundError:
        pass

    # Prevent multiple simultaneous token requests
    if st.session_state.get("token_generating", False):
        st.warning("⏳ Token generation in progress...")
        return None

    st.session_state.token_generating = True

    # Auto-generate token using TOTP — only once per session
    token, error = generate_token()
    st.session_state.token_generating = False

    if not token:
        st.error(f"❌ Token generation failed: {error}")
        return None

    st.session_state.access_token = token
    cid = get_secret("FYERS_CLIENT_ID") or CLIENT_ID
    return fyersModel.FyersModel(client_id=cid, token=token, log_path="")

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

st.set_page_config(page_title="SENSEX/NIFTY Spread Dashboard", page_icon="📈", layout="wide")

st.markdown("""
<style>
    .stApp { background-color: #0e0e0e; color: #ffffff; }
    section[data-testid="stSidebar"] { background-color: #111122; }
    .metric-card { background: #1a1a2e; border-radius: 10px; padding: 15px; text-align: center; border: 1px solid #2a2a4a; margin-bottom: 8px; }
    .ce-value  { color: #ff4444; font-size: 26px; font-weight: bold; }
    .pe-value  { color: #44ff88; font-size: 26px; font-weight: bold; }
    .diff-pos  { color: #ff4444; font-size: 26px; font-weight: bold; }
    .diff-neg  { color: #44ff88; font-size: 26px; font-weight: bold; }
    .label     { color: #888;    font-size: 12px; margin-bottom: 4px; }
    .sublabel  { color: #555;    font-size: 11px; margin-top: 4px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📊 Spread Settings")
    st.markdown("### 🔵 Leg 1 (SENSEX)")
    sensex_exchange   = st.selectbox("Exchange", ["BSE", "NSE"], index=0, key="sx_exch")
    sensex_underlying = st.selectbox("Underlying", ["SENSEX", "BANKEX"], index=0, key="sx_under")
    col1, col2 = st.columns(2)
    with col1: sensex_ce_expiry = st.text_input("CE Expiry", value="260312", key="sx_ce_exp", help="YYMMDD")
    with col2: sensex_pe_expiry = st.text_input("PE Expiry", value="260312", key="sx_pe_exp")
    col3, col4 = st.columns(2)
    with col3: sensex_ce_strike = st.number_input("CE Strike", value=80000, step=100, key="sx_ce_str")
    with col4: sensex_pe_strike = st.number_input("PE Strike", value=80000, step=100, key="sx_pe_str")

    st.divider()
    st.markdown("### 🟠 Leg 2 (NIFTY)")
    nifty_exchange   = st.selectbox("Exchange", ["NSE", "BSE"], index=0, key="nf_exch")
    nifty_underlying = st.selectbox("Underlying", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"], index=0, key="nf_under")
    col5, col6 = st.columns(2)
    with col5: nifty_ce_expiry = st.text_input("CE Expiry", value="260310", key="nf_ce_exp", help="YYMMDD")
    with col6: nifty_pe_expiry = st.text_input("PE Expiry", value="260310", key="nf_pe_exp")
    col7, col8 = st.columns(2)
    with col7: nifty_ce_strike = st.number_input("CE Strike", value=24800, step=50, key="nf_ce_str")
    with col8: nifty_pe_strike = st.number_input("PE Strike", value=24800, step=50, key="nf_pe_str")

    st.divider()
    st.markdown("### ⚙️ Formula & Display")
    multiplier      = st.number_input("Leg 2 Multiplier", value=3.3, step=0.1, min_value=0.1, key="mult")
    candle_interval = st.selectbox("Candle Interval (min)", [1, 3, 5, 10, 15, 30, 60], index=2)
    selected_date   = st.date_input("📅 Date", value=date.today())
    date_str        = selected_date.strftime("%Y-%m-%d")

    st.divider()
    show_raw     = st.checkbox("Show Raw Prices Chart", value=False)
    show_diff    = st.checkbox("Show CE+PE Total Spread", value=True)
    auto_refresh = st.checkbox("Auto Refresh", value=True)
    refresh_secs = st.slider("Refresh every (sec)", 5, 60, REFRESH_SECONDS)

    st.divider()
    fetch_btn = st.button("🔄 Fetch / Refresh Data", use_container_width=True, type="primary")

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────

st.title("📊 SENSEX / NIFTY Synthetic Spread")
st.caption(f"{sensex_exchange}:{sensex_underlying} − ({nifty_exchange}:{nifty_underlying} × {multiplier}) | {candle_interval}min | {date_str}")

sym_sx_ce = build_symbol(sensex_exchange, sensex_underlying, sensex_ce_expiry, "C", int(sensex_ce_strike))
sym_sx_pe = build_symbol(sensex_exchange, sensex_underlying, sensex_pe_expiry, "P", int(sensex_pe_strike))
sym_nf_ce = build_symbol(nifty_exchange,  nifty_underlying,  nifty_ce_expiry,  "C", int(nifty_ce_strike))
sym_nf_pe = build_symbol(nifty_exchange,  nifty_underlying,  nifty_pe_expiry,  "P", int(nifty_pe_strike))

with st.expander("🔍 Active Symbols (click to verify)"):
    c1, c2, c3, c4 = st.columns(4)
    c1.code(sym_sx_ce, language=None)
    c2.code(sym_sx_pe, language=None)
    c3.code(sym_nf_ce, language=None)
    c4.code(sym_nf_pe, language=None)

# ─────────────────────────────────────────────
# FETCH LIVE DATA
# ─────────────────────────────────────────────

def fetch_live_data():
    fyers = get_fyers_client()
    if fyers is None:
        return pd.DataFrame()

    with st.spinner("Fetching option prices from Fyers..."):
        df_sx_ce = fetch_candles(fyers, sym_sx_ce, candle_interval, date_str)
        df_sx_pe = fetch_candles(fyers, sym_sx_pe, candle_interval, date_str)
        df_nf_ce = fetch_candles(fyers, sym_nf_ce, candle_interval, date_str)
        df_nf_pe = fetch_candles(fyers, sym_nf_pe, candle_interval, date_str)

    if any(df.empty for df in [df_sx_ce, df_sx_pe, df_nf_ce, df_nf_pe]):
        st.warning("⚠️ One or more symbols returned no data. Check expiry/strike values in sidebar.")
        return pd.DataFrame()

    for df_ in [df_sx_ce, df_sx_pe, df_nf_ce, df_nf_pe]:
        df_ = df_[~df_.index.duplicated(keep="last")]

    df = pd.DataFrame({
        "sensex_ce": df_sx_ce["close"],
        "sensex_pe": df_sx_pe["close"],
        "nifty_ce" : df_nf_ce["close"],
        "nifty_pe" : df_nf_pe["close"],
    }).dropna()

    df["ce_spread"] = df["sensex_ce"] - (df["nifty_ce"] * multiplier)
    df["pe_spread"] = df["sensex_pe"] - (df["nifty_pe"] * multiplier)
    df["diff"]      = df["ce_spread"] + df["pe_spread"]
    return df

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────

if "df" not in st.session_state:
    st.session_state.df = pd.DataFrame()

if fetch_btn or st.session_state.df.empty:
    st.session_state.df = fetch_live_data()

df = st.session_state.df

# ─────────────────────────────────────────────
# RENDER DASHBOARD
# ─────────────────────────────────────────────

if df.empty:
    st.info("👈 Set your strikes and expiry in the sidebar, then click **Fetch / Refresh Data**.")
else:
    latest   = df.iloc[-1]
    ce_val   = latest["ce_spread"]
    pe_val   = latest["pe_spread"]
    diff_val = latest["diff"]
    updated  = df.index[-1].strftime("%H:%M:%S")
    is_today = date_str == date.today().strftime("%Y-%m-%d")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(f"<div class='metric-card'><div class='label'>🔴 CE Spread</div><div class='ce-value'>{ce_val:.2f}</div><div class='sublabel'>{sensex_underlying} CE {int(sensex_ce_strike)} − {nifty_underlying} CE {int(nifty_ce_strike)}×{multiplier}</div></div>", unsafe_allow_html=True)
    with col2:
        st.markdown(f"<div class='metric-card'><div class='label'>🟢 PE Spread</div><div class='pe-value'>{pe_val:.2f}</div><div class='sublabel'>{sensex_underlying} PE {int(sensex_pe_strike)} − {nifty_underlying} PE {int(nifty_pe_strike)}×{multiplier}</div></div>", unsafe_allow_html=True)
    with col3:
        diff_cls = "diff-pos" if diff_val >= 0 else "diff-neg"
        st.markdown(f"<div class='metric-card'><div class='label'>🟠 CE + PE Total</div><div class='{diff_cls}'>{diff_val:+.2f}</div><div class='sublabel'>CE Spread plus PE Spread</div></div>", unsafe_allow_html=True)
    with col4:
        st.markdown(f"<div class='metric-card'><div class='label'>🕐 Last Candle</div><div style='font-size:20px;font-weight:bold;color:#aaa'>{updated}</div><div class='sublabel'>{'🔴 LIVE' if is_today else '📂 Historical'} | {len(df)} candles</div></div>", unsafe_allow_html=True)

    st.divider()

    fig = make_subplots(
        rows=2 if show_diff else 1, cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3] if show_diff else [1.0],
        vertical_spacing=0.04
    )

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
        fig.add_trace(go.Scatter(x=df.index, y=df["diff"], name="4 Leg", line=dict(color="#ffaa00", width=2), hovertemplate="%{x|%H:%M}<br>4 Leg: %{y:.2f}<extra></extra>"), row=2, col=1)
        fig.add_hline(y=0, line_dash="dash", line_color="#444", row=2, col=1)

    fig.update_layout(
        height=560, plot_bgcolor="#0e0e0e", paper_bgcolor="#0e0e0e",
        font=dict(color="#cccccc"), legend=dict(bgcolor="#1a1a2e", bordercolor="#333"),
        hovermode="x unified", margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(gridcolor="#1a1a1a"), yaxis=dict(gridcolor="#1a1a1a", title="Spread (₹)"),
    )
    if show_diff:
        fig.update_yaxes(gridcolor="#1a1a1a", title_text="4 Leg", row=2, col=1)
        fig.update_xaxes(gridcolor="#1a1a1a", row=2, col=1)

    st.plotly_chart(fig, use_container_width=True)

    if show_raw:
        st.subheader("📋 Raw Option Prices")
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=df.index, y=df["sensex_ce"], name=f"{sensex_underlying} CE", line=dict(color="#ff6666")))
        fig2.add_trace(go.Scatter(x=df.index, y=df["sensex_pe"], name=f"{sensex_underlying} PE", line=dict(color="#66ff99")))
        fig2.add_trace(go.Scatter(x=df.index, y=df["nifty_ce"],  name=f"{nifty_underlying} CE",  line=dict(color="#ff9999", dash="dot")))
        fig2.add_trace(go.Scatter(x=df.index, y=df["nifty_pe"],  name=f"{nifty_underlying} PE",  line=dict(color="#99ffbb", dash="dot")))
        fig2.update_layout(height=320, plot_bgcolor="#0e0e0e", paper_bgcolor="#0e0e0e", font=dict(color="#cccccc"), hovermode="x unified", margin=dict(l=20, r=20, t=10, b=20), xaxis=dict(gridcolor="#1a1a1a"), yaxis=dict(gridcolor="#1a1a1a", title="Price (₹)"))
        st.plotly_chart(fig2, use_container_width=True)

    with st.expander("📄 Raw Data Table"):
        st.dataframe(
            df[["sensex_ce","sensex_pe","nifty_ce","nifty_pe","ce_spread","pe_spread","diff"]]
            .rename(columns={"sensex_ce": f"{sensex_underlying} CE", "sensex_pe": f"{sensex_underlying} PE", "nifty_ce": f"{nifty_underlying} CE", "nifty_pe": f"{nifty_underlying} PE"})
            .round(2).sort_index(ascending=False), use_container_width=True
        )

# ─────────────────────────────────────────────
# AUTO REFRESH
# ─────────────────────────────────────────────

if auto_refresh and date_str == date.today().strftime("%Y-%m-%d") and not df.empty:
    time.sleep(refresh_secs)
    st.session_state.df = fetch_live_data()
    st.rerun()
