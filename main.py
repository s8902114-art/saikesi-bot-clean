import os
import time
import hmac
import hashlib
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from flask import Flask, request, jsonify
import requests
import threading

app = Flask(__name__)

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def verify_discord_signature(req):
    signature = req.headers.get("X-Signature-Ed25519", "")
    timestamp = req.headers.get("X-Signature-Timestamp", "")
    body = req.data.decode("utf-8")
    try:
        verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        verify_key.verify(f"{timestamp}{body}".encode(), bytes.fromhex(signature))
        return True
    except BadSignatureError:
        return False

@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200

@app.route("/interactions", methods=["POST"])
def interactions():
    if not verify_discord_signature(request):
        return "Invalid signature", 401
    data = request.json
    if data.get("type") == 1:
        return jsonify({"type": 1})
    return jsonify({"type": 4, "data": {"content": "OK"}})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
