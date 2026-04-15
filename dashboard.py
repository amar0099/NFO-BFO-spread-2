# FIXED VERSION — Key changes:
# 1. Proper token refresh (no permanent cache)
# 2. Removed duplicate cache decorator
# 3. Removed dependency on access_token.txt
# 4. Added basic retry for API calls

import os
import base64
import pyotp
import requests
import pandas as pd
import streamlit as st
import time
from datetime import date
from urllib.parse import parse_qs, urlparse
from fyers_apiv3 import fyersModel

# CONFIG
CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "YOUR_APP_ID-100")
SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "YOUR_SECRET_KEY")

# ---------------- TOKEN MANAGEMENT ----------------

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
    client_id = get_secret("FYERS_CLIENT_ID")
    secret_key = get_secret("FYERS_SECRET_KEY")
    username = get_secret("FYERS_USERNAME")
    pin = get_secret("FYERS_PIN")
    totp_key = get_secret("FYERS_TOTP_KEY")
    redirect_uri = "http://127.0.0.1:8080/"

    try:
        s = requests.Session()

        r1 = s.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
                    json={"fy_id": b64(username), "app_id": "2"})
        r1d = r1.json()

        totp_code = pyotp.TOTP(totp_key).now()

        r2 = s.post("https://api-t2.fyers.in/vagator/v2/verify_otp",
                    json={"request_key": r1d["request_key"], "otp": totp_code})
        r2d = r2.json()

        r3 = s.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
                    json={"request_key": r2d["request_key"], "identity_type": "pin", "identifier": b64(pin)})
        r3d = r3.json()

        app_id = client_id.split("-")[0]

        r4 = s.post("https://api-t1.fyers.in/api/v3/token", json={
            "fyers_id": username,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "appType": "100",
            "response_type": "code"
        }, headers={"Authorization": f"Bearer {r3d['data']['access_token']}"})

        r4d = r4.json()
        url_key = "Url" if "Url" in r4d else "url"
        auth_code = parse_qs(urlparse(r4d[url_key]).query).get("auth_code", [None])[0]

        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code"
        )
        session.set_token(auth_code)
        token = session.generate_token().get("access_token")

        return token, None

    except Exception as e:
        return None, str(e)


def get_shared_token():
    """Token with expiry handling"""

    if "token_data" not in st.session_state:
        token, error = generate_token()
        if not token:
            raise RuntimeError(error)
        st.session_state["token_data"] = {
            "token": token,
            "time": time.time()
        }
        return token

    # Refresh every 6 hours
    if time.time() - st.session_state["token_data"]["time"] > 6 * 3600:
        token, error = generate_token()
        if not token:
            raise RuntimeError(error)
        st.session_state["token_data"] = {
            "token": token,
            "time": time.time()
        }

    return st.session_state["token_data"]["token"]


def get_fyers_client():
    try:
        token = get_shared_token()
        return fyersModel.FyersModel(client_id=CLIENT_ID, token=token, log_path="")
    except Exception as e:
        st.error(f"Login failed: {e}")
        return None

# ---------------- API FETCH WITH RETRY ----------------

def fetch_history_with_retry(fyers, data, retries=3):
    for _ in range(retries):
        try:
            res = fyers.history(data=data)
            if res.get("s") == "ok":
                return res
        except Exception:
            pass
        time.sleep(1)
    return {"s": "error"}


def fetch_candles(fyers, symbol, interval, date_str=None):
    if date_str is None:
        date_str = date.today().strftime("%Y-%m-%d")

    res = fetch_history_with_retry(fyers, {
        "symbol": symbol,
        "resolution": str(interval),
        "date_format": "1",
        "range_from": date_str,
        "range_to": date_str,
        "cont_flag": "1"
    })

    if res.get("s") != "ok":
        return pd.DataFrame()

    df = pd.DataFrame(res["candles"], columns=["ts","o","h","l","c","v"])
    df["dt"] = pd.to_datetime(df["ts"], unit="s")
    return df.set_index("dt")

# ---------------- STREAMLIT UI ----------------

st.title("Fixed Fyers Dashboard")

if st.button("Refresh Token"):
    st.session_state.pop("token_data", None)
    st.rerun()

fyers = get_fyers_client()

if fyers:
    symbol = st.text_input("Symbol", "NSE:NIFTY50-INDEX")
    interval = st.selectbox("Interval", [1,5,15])

    if st.button("Fetch Data"):
        df = fetch_candles(fyers, symbol, interval)
        if df.empty:
            st.error("No data")
        else:
            st.dataframe(df.tail())
