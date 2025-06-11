import json
import sqlite3
import requests
from flask import Flask, request
from datetime import datetime

# 設定読み込み
config = json.load(open("line.json", encoding="utf-8"))
LINE_ACCESS_TOKEN = config["line_bot_token"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
}

# Cloudflare Tunnel 公開URL
CLOUDFLARE_BASE = "https://photo.minepus.net"

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

# 🔁 FlaskローカルAPI呼び出し（10.0.0.1側）
def request_images(user_id):
    res = requests.post("https://photo.minepus.net/generate", json={"user_id": user_id})
    return res.json()["image1"], res.json()["image2"], res.json()["generation"]

def reply_message(token, messages):
    requests.post(LINE_REPLY_URL, headers=HEADERS, json={
        "replyToken": token,
        "messages": messages
    })

def push_message(to, messages):
    requests.post(LINE_PUSH_URL, headers=HEADERS, json={
        "to": to,
        "messages": messages
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.json

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
            img1, img2, gen = request_images(user_id)

            # ✅ Cloudflare URLに変換
            img1 = img1.replace("http://10.0.0.1:5000", CLOUDFLARE_BASE)
            img2 = img2.replace("http://10.0.0.1:5000", CLOUDFLARE_BASE)

            save_generated(user_id, gen, img1, img2)

            reply_message(reply_token, [
                {"type": "text", "text": f"第{gen}世代の画像です。どちらがAに似ていますか？1または2で選んでください。"},
                {"type": "image", "originalContentUrl": img1, "previewImageUrl": img1},
                {"type": "image", "originalContentUrl": img2, "previewImageUrl": img2}
            ])
            reply_message(reply_token, [{"type": "text", "text": "画像を送信しました。"}])

        elif text in ["1", "2"]:
            update_selection(user_id, int(text))

            # 🔁 画像選択情報を画像生成サーバーに通知
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT generation, image1, image2 FROM user_selection 
                         WHERE user_id = ? AND selected IS NOT NULL
                         ORDER BY timestamp DESC LIMIT 1''', (user_id,))
            row = c.fetchone()
            conn.close()

            if row:
                generation, img1, img2 = row
                requests.post("https://photo.minepus.net/select", json={
                    "user_id": user_id,
                    "generation": generation,
                    "selected": int(text),
                    "img1": img1,
                    "img2": img2
                })

            reply_message(reply_token, [{"type": "text", "text": f"{text} を選択として記録しました。"}])

        elif text == "アルゴリズムリセット":
            reset_algorithm()
            reply_message(reply_token, [{"type": "text", "text": "アルゴリズム履歴をリセットしました。"}])

        else:
            reply_message(reply_token, [{"type": "text", "text": "「アルゴリズム」「1」「2」「アルゴリズムリセット」のいずれかを送信してください。"}])

    return "OK", 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050)
