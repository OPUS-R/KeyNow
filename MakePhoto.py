import os
import json
import sqlite3
import requests
import time
import logging
from flask import Flask, request
from datetime import datetime

# ログ設定
logging.basicConfig(
    filename="linebot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# LINEAPI認証関連
config = json.load(open("line.json", encoding="utf-8"))
LINE_ACCESS_TOKEN = config["line_bot_token"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
}
CLOUDFLARE_BASE = config["CLOUD"]



# DB設定
DB_PATH = "ga_selection.db"
app = Flask(__name__)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_selection (
        user_id TEXT,
        generation INTEGER,
        image1 TEXT,
        image2 TEXT,
        selected INTEGER,
        timestamp TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

def save_generated(user_id, generation, img1, img2):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO user_selection (user_id, generation, image1, image2, selected, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, generation, img1, img2, None, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_selection(user_id, selected):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''UPDATE user_selection 
                 SET selected = ? 
                 WHERE user_id = ? AND selected IS NULL
                 ORDER BY timestamp DESC LIMIT 1''',
              (selected, user_id))
    conn.commit()
    conn.close()

def reset_algorithm():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM user_selection")
    conn.commit()
    conn.close()

def request_images(user_id):
    res = requests.post("https://photo.minepus.net/generate", json={"user_id": user_id})
    logger.info(f"Requested image generation for user {user_id}")
    if res.status_code == 403:
        raise Exception("locked")
    return res.json()["image1"], res.json()["image2"], res.json()["generation"]

def wait_for_images(generation, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{CLOUDFLARE_BASE}/status", params={"gen": str(generation)}, timeout=1)
            if r.status_code == 200 and r.json().get("ready"):
                return True
        except Exception as e:
            logger.warning(f"Status check failed: {e}")
        time.sleep(1)
    return False


def reply_message(token, messages):
    res = requests.post(LINE_REPLY_URL, headers=HEADERS, json={
        "replyToken": token,
        "messages": messages
    })
    logger.info(f"Reply sent: {res.status_code} {res.text}")

def push_message(to, messages):
    requests.post(LINE_PUSH_URL, headers=HEADERS, json={
        "to": to,
        "messages": messages
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.json
    logger.info(f"Received webhook: {body}")

    for event in body["events"]:
        if event["type"] != "message":
            continue

        msg = event["message"]
        user_id = event["source"]["userId"]
        reply_token = event["replyToken"]

        if msg["type"] != "text":
            reply_message(reply_token, [{"type": "text", "text": "テキストで操作してください。"}])
            continue

        text = msg["text"].strip()

        if text == "アルゴリズム":
            try:
                # 画像生成をリクエスト
                res = requests.post(f"{CLOUDFLARE_BASE}/generate", json={"user_id": user_id})

                if res.status_code == 403:
                    reply_message(reply_token, [{
                        "type": "text",
                        "text": "現在他のユーザーが操作中です。操作完了までお待ちください"
                    }])
                    continue

                if res.status_code != 200:
                    reply_message(reply_token, [{
                        "type": "text",
                        "text": "画像生成に失敗しました。もう一度お試しください。"
                    }])
                    continue

                generation = res.json().get("generation", 0)
                img1_url = f"{CLOUDFLARE_BASE}/images/{generation}/1.jpg"
                img2_url = f"{CLOUDFLARE_BASE}/images/{generation}/2.jpg"

                if not wait_for_images(generation):
                    reply_message(reply_token, [{
                        "type": "text",
                        "text": "画像の準備に時間がかかっています。しばらくしてから再度お試しください。"
                    }])
                    continue

                # DBに保存など（任意）
                save_generated(user_id, generation, img1_url, img2_url)

                # 返信メッセージ送信
                reply_message(reply_token, [
                    {"type": "text", "text": f"第{generation}世代の画像です。どちらがAに似ていますか？1または2で選んでください。"},
                    {"type": "image", "originalContentUrl": img1_url, "previewImageUrl": img1_url},
                    {"type": "image", "originalContentUrl": img2_url, "previewImageUrl": img2_url}
                ])
            except Exception as e:
                logger.error(f"画像生成エラー: {e}")
                reply_message(reply_token, [{
                    "type": "text",
                    "text": "エラーが発生しました。もう一度お試しください。"
                }])


        elif text in ["1", "2"]:
            selected = int(text)
            update_selection(user_id, selected)

            try:
                res = requests.post("https://photo.minepus.net/select", json={
                    "user_id": user_id,
                    "selected": selected
                })
                res.raise_for_status()
                reply_message(reply_token, [{"type": "text", "text": f"{text} を選択として記録しました。"}])
            except Exception as e:
                logger.error(f"Selection sync error: {e}")
                reply_message(reply_token, [{"type": "text", "text": "選択の同期に失敗しました。"}])

        elif text == "アルゴリズムリセット":
            reset_algorithm()

            try:
                res = requests.post("https://photo.minepus.net/reset", json={
                    "user_id": user_id
                })
                res.raise_for_status()
                reply_message(reply_token, [{"type": "text", "text": "アルゴリズム履歴をリセットしました。"}])
            except Exception as e:
                logger.error(f"Reset sync error: {e}")
                reply_message(reply_token, [{"type": "text", "text": "リセットに失敗しました。"}])

    return "OK", 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050)
