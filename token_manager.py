import time
import json
import os
from dashboard import generate_token  # reuse your existing function

TOKEN_FILE = "token_store.json"
EXPIRY_SECONDS = 6 * 3600  # 6 hours


def get_token():
    # Try reading existing token
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)

            if time.time() - data["time"] < EXPIRY_SECONDS:
                return data["token"]

        except Exception:
            pass

    # Generate new token (ONLY place OTP is called)
    token, err = generate_token()
    if not token:
        raise Exception(f"Token generation failed: {err}")

    data = {
        "token": token,
        "time": time.time()
    }

    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)

    return token
