import os
import json
import sqlite3
import logging
import asyncio
import httpx
from datetime import datetime, date
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
from datetime import datetime, date, timedelta

#ログ設定###############################################################################################################あ
logging.basicConfig(
    filename="keynow.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S"
)
logger = logging.getLogger(__name__)
logger.info("Starting KeyNow bot")

#スケジューラー用ログ上書き
aps_logger = logging.getLogger("apscheduler")
aps_handler = logging.FileHandler("scheduler.log", encoding="utf-8")
aps_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y/%m/%d %H:%M:%S"
))
aps_logger.setLevel(logging.INFO)
aps_logger.addHandler(aps_handler)
aps_logger.propagate = False  # 親ロガーに流さない

#ログ履歴保管用(DB30day)
def log_key_action(action, key_name, user_name):
    now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    INSERT INTO key_logs (action, key_name, user_name, timestamp)
    VALUES (?, ?, ?, ?)
    """, (action, key_name, user_name, now))
    conn.commit()
    conn.close()
    logger.info(f"Logged action: {action} - {key_name} by {user_name}")

#ログ履歴送信
async def send_history(reply_token: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = """
    SELECT action, key_name, user_name, timestamp
    FROM key_logs
    WHERE timestamp >= datetime('now', '-30 day')
    ORDER BY timestamp DESC
    """
    c.execute(query)
    rows = c.fetchall()
    conn.close()

    if not rows:
        reply = "過去30日間の操作履歴はありません。"
    else:
        logs = [f"{row[3]} - {row[1]}: {row[2]} が {row[0]}" for row in rows]
        reply = "過去30日間の履歴:\n" + "\n".join(logs)

    await send_line_message(reply_token, reply)


#Google Sheets 認証######################################################################################################
try:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name("GCOA.json", scope)
    client = gspread.authorize(creds)
    logger.info("Google Sheets 認証成功")

    # シート指定と確認
    debt_spreadsheet = client.open("名簿DB")
    sheet1 = debt_spreadsheet.worksheet("名簿")
    logger.info("シート '名簿' に正常にアクセスしました")

    reserve_spreadsheet = client.open("KeyNow")
    sheet2 = reserve_spreadsheet.worksheet("予約")
    logger.info("シート '予約' に正常にアクセスしました")

except Exception as e:
    logger.error(f"Google Sheets 認証またはシートアクセスに失敗: {str(e)}")
    exit(1)

#Google Drive 認証#######################################################################################################
gauth = GoogleAuth()
gauth.credentials = creds
drive = GoogleDrive(gauth)

#スプレッドシート指定#######################################################################################################
debt_spreadsheet = client.open("名簿DB")
sheet1 = debt_spreadsheet.worksheet("名簿")
reserve_spreadsheet = client.open("KeyNow")
sheet2 = reserve_spreadsheet.worksheet("予約")

# LINEAPI###############################################################################################################
config = json.load(open("line.json", encoding="utf-8"))


LINE_ACCESS_TOKEN = config["line_bot_token"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

#Flask / APScheduler 初期化##############################################################################################
app = Flask(__name__)

def run_reset_key_holders():
    asyncio.run(reset_key_holders())

def run_notify_overdue_keys():
    asyncio.run(notify_overdue_keys())

def start_scheduler():  # APScheduler 起動用関数
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(run_reset_key_holders, 'cron', hour=0, minute=0)#0:00reset
        scheduler.add_job(run_notify_overdue_keys, 'interval', minutes=3)#sheet time reset
        scheduler.start()
        logger.info("APScheduler 起動成功")
    except Exception as e:
        logger.error(f"APScheduler 起動失敗: {str(e)}")

# SQLite 初期化##########################################################################################################
DB_PATH = 'key_reservation.db'#鍵保管用DBの名前
conn = None

#DB接続
def get_db_connection():
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        logger.info("SQLite DB connection established")
        return conn
    except Exception as e:
        logger.error(f"データベース接続エラー: {str(e)}")
        return None

#DB作成
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        line_id TEXT PRIMARY KEY,
        student_no TEXT,
        name TEXT
    )""")
    c.execute("CREATE TABLE IF NOT EXISTS groups ( group_id TEXT PRIMARY KEY )")
    c.execute("""
    CREATE TABLE IF NOT EXISTS key_holders (
        key_name TEXT PRIMARY KEY,
        holder_id TEXT,
        borrow_time TEXT
    )""")
    # 鍵操作履歴テーブル
    c.execute("""
    CREATE TABLE IF NOT EXISTS key_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT,
        key_name TEXT,
        user_name TEXT,
        timestamp TEXT
    )""")
    conn.commit()
    conn.close()
    logger.info("KeyLogのDBの初期化完了")

init_db()

# LINE メッセージ送信######################################################################################################

#Reply_tokenを使用=無料
async def send_line_message(reply_token: str, message: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": message}]
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        for _ in range(3):
            try:
                resp = await client.post(LINE_REPLY_URL, headers=headers, json=payload)
                if resp.status_code == 200:
                    logger.info(f"LINEメッセージ送信成功: {message}")
                    return
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"LINEメッセージ送信失敗: {str(e)}")
    logger.error(f"LINEメッセージ送信に失敗: {message}")

#有料push
async def push_line_message(user_id: str, message: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}]
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(LINE_PUSH_URL, headers=headers, json=payload)
            if resp.status_code == 200:
                logger.info(f"LINEプッシュ送信成功: {message} (to {user_id})")
            else:
                logger.error(f"LINEプッシュ送信失敗: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"LINEプッシュ送信エラー: {str(e)}")

# 未返却通知用スケジューラ #################################################################################################
async def notify_overdue_keys():
    now = datetime.now().strftime("%H:%M")
    today = datetime.today().strftime("%Y/%m/%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT key_name, holder_id FROM key_holders")
    rows = c.fetchall()

    # ユーザー単位で鍵をまとめる
    overdue_dict = {}
    for key_name, holder_id in rows:
        # デフォルト終了時間
        default_end_time = "20:55"
        end_time = default_end_time

        # シートでその日の終了時間を取得
        date_cells = sheet2.findall(today)
        if date_cells:
            for cell in date_cells:
                time_range = sheet2.cell(cell.row, cell.col + 1).value
                try:
                    _, end_hour = time_range.split("-")
                    end_hour = int(end_hour)
                    if end_hour < 21:
                        end_time = f"{end_hour}:00"
                except Exception:
                    continue

        if now > end_time:
            overdue_dict.setdefault(holder_id, []).append(key_name)

    # 通知処理
    for holder_id, key_list in overdue_dict.items():
        try:
            user_name = get_user_name(holder_id)
            notified_keys = []
            for key_name in key_list:
                if not already_notified_today(holder_id, key_name):
                    notified_keys.append(key_name)

            if not notified_keys:
                continue  # 通知済みかチェック

            key_str = "・".join(notified_keys)
            message = f"{key_str}の返却期限が過ぎています。{user_name} さん、返却してください。"
            message_author = f"{key_str}の返却期限が過ぎています。{user_name} さんへ通知しました。"
            await push_line_message(holder_id, message)
            await push_to_authenticated_groups(message_author)
            for key_name in notified_keys:
                log_key_action("通知", key_name, user_name)
            logger.warning(f"通知:{key_str} の返却期限が過ぎています。. {user_name} と認証済みグループに通知しました。")

        except Exception as e:
            logger.error(f"未返却通知失敗（{holder_id}）: {str(e)}")


def already_notified_today(user_id, key_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = date.today().strftime("%Y/%m/%d")
    user_name = get_user_name(user_id)
    c.execute("""
        SELECT 1 FROM key_logs 
        WHERE action='通知' AND key_name=? AND user_name=? AND timestamp LIKE ?
    """, (key_name, user_name, f"{today}%"))
    result = c.fetchone()
    conn.close()
    return result is not None



# Webhook エンドポイント###################################################################################################
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    logger.info(f"Webhook受信: {data}")

    for event in data.get("events", []):
        if event.get("type") != "message" or event["message"].get("type") != "text":
            continue

        reply_token = event.get("replyToken")
        source = event["source"]
        user_id = source.get("userId")
        group_id = source.get("groupId")
        text = event["message"]["text"].strip()

        conn = get_db_connection()
        c = conn.cursor()


        # 学籍番号登録
        if user_id and text.lower().startswith("番号:"):
            no_upper = text.split("番号:")[1].strip().upper()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()

            # 学籍番号チェック
            c.execute("SELECT student_no, name FROM users WHERE line_id=?", (user_id,))
            if c.fetchone():
                reply = "すでに登録済みです。"
            else:
                try:
                    # sheet1から学籍番号を検索
                    found_cells = sheet1.findall(no_upper)
                    if found_cells:
                        # 最初に見つかったセルを使用
                        cell = found_cells[0]
                        name = sheet1.cell(cell.row, cell.col + 1).value  # 右隣のセルを名前として取得
                        c.execute("INSERT INTO users(line_id, student_no, name) VALUES (?, ?, ?)", (user_id, no_upper, name))
                        conn.commit()
                        reply = f"登録完了：{name}（{no_upper}）"
                    else:
                        reply = "学籍番号が見つかりません。"
                except Exception as e:
                    reply = f"エラーが発生しました：{str(e)}"
            conn.close()
            asyncio.run(send_line_message(reply_token, reply))
            continue

    #グループ認証
        if group_id and text.startswith("OPUS#2024&"):#認証番号
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO groups(group_id) VALUES (?)", (group_id,))
            conn.commit()
            conn.close()
            reply = "このグループを認証済みに登録しました。"
            asyncio.run(send_line_message(reply_token, reply))
            continue

    #認証グループ削除
        if group_id and text.strip() == "OPUS&Delete":
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM groups WHERE group_id=?", (group_id,))
            conn.commit()
            conn.close()
            reply = "このグループを認証済みグループから削除しました。"
            asyncio.run(send_line_message(reply_token, reply))
            continue



    # 鍵管理（借りる、返却、引き継ぎ）
        parts = text.split()
        logger.info(f"Command parsed: {parts}")

        if len(parts) == 2 and parts[0] in ["借りる", "返却", "引き継ぎ"]:
            action, key_name = parts
            logger.info(f"メッセージを受信: {action}, 鍵種類: {key_name}")
            valid_keys = ["音倉", "音練", "両方"]
            if key_name not in valid_keys:
                reply = "鍵の種類は「音倉」「音練」「両方」のいずれかを記入してください。"
                asyncio.run(send_line_message(reply_token, reply))
                continue

            # 「両方」の場合、音倉と音練をそれぞれ処理する
            keys_to_process = ["音倉", "音練"] if key_name == "両方" else [key_name]
            logger.info(f"Keys to process: {keys_to_process}")
            #学籍番号登録チェック
            if not is_user_registered(user_id):
                reply = "学籍番号が登録されていません。まず「番号:あなたの学籍番号」で登録してください。"
                asyncio.run(send_line_message(reply_token, reply))
                continue

            now = datetime.now().strftime("%Y/%m/%d %H:%M")
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()

                for key_name in keys_to_process:
                    try:
                        # 借りる処理
                        if action == "借りる":
                            logger.info(f"借りる操作開始: {key_name}")
                            # 両方借りる場合、両方鍵があるか確認
                            if len(keys_to_process) == 2 and key_name == "音倉":
                                c.execute("SELECT holder_id FROM key_holders WHERE key_name IN (?, ?)", ("音倉", "音練"))
                                holders = c.fetchall()
                                if len(holders) > 0:
                                    # 既にどちらかが借りられている場合
                                    borrowed_keys = [h[0] for h in holders]
                                    reply = "音倉または音練のどちらかが既に借りられています。"
                                    logger.warning(f"借りる操作失敗: 音倉・音練のどちらかが既に借りられている: {borrowed_keys}")
                                    asyncio.run(send_line_message(reply_token, reply))
                                    continue

                                # 両方を一括で借りる
                                user_name = get_user_name(user_id)
                                c.execute("INSERT INTO key_holders(key_name, holder_id, borrow_time) VALUES (?, ?, ?)",
                                          ("音倉", user_id, now))
                                c.execute("INSERT INTO key_holders(key_name, holder_id, borrow_time) VALUES (?, ?, ?)",
                                          ("音練", user_id, now))
                                conn.commit()
                                log_key_action("借りる", "音倉・音練", user_name)
                                reply = f"音倉・音練 を {user_name} さんが借りました。"
                                logger.info(f"両方借りる操作成功: 音倉・音練 を {user_name} さんが借りました")
                                asyncio.run(send_line_message(reply_token, reply))
                                asyncio.run(push_to_authenticated_groups(reply))
                                break

                            # 個別の鍵借りる処理
                            c.execute("SELECT holder_id FROM key_holders WHERE key_name=?", (key_name,))
                            result = c.fetchone()

                            if result:
                                reply = f"{key_name} は既に借りられています。"
                                logger.warning(f"借りる操作失敗: {key_name} は既に借りられています")
                            else:
                                # 鍵を借りる処理
                                user_name = get_user_name(user_id)
                                c.execute("INSERT INTO key_holders(key_name, holder_id, borrow_time) VALUES (?, ?, ?)",
                                          (key_name, user_id, now))
                                conn.commit()
                                log_key_action("借りる", key_name, user_name)
                                reply = f"{key_name} を {user_name} さんが借りました。"
                                logger.info(f"借りる操作成功: {key_name} を {user_name} さんが借りました")

                            # メッセージ送信
                            asyncio.run(send_line_message(reply_token, reply))
                            asyncio.run(push_to_authenticated_groups(reply))
                        # 返却処理
                        elif action == "返却":
                            logger.info(f"返却操作開始: {key_name}")
                            # 両方返却の場合、事前に所有者チェック
                            if len(keys_to_process) == 2 and key_name == "音倉":
                                c.execute("SELECT holder_id FROM key_holders WHERE key_name IN (?, ?)", ("音倉", "音練"))
                                holders = c.fetchall()
                                if len(holders) != 2 or holders[0][0] != holders[1][0]:
                                    reply = "音倉と音練は同じ所有者でないため、同時に返却できません。"
                                    logger.warning(f"返却操作失敗: 音倉と音練の所有者が異なる")
                                    asyncio.run(send_line_message(reply_token, reply))
                                    continue

                                # 両方の鍵を一括で返却
                                c.execute("DELETE FROM key_holders WHERE key_name IN (?, ?)", ("音倉", "音練"))
                                conn.commit()
                                user_name = get_user_name(user_id)
                                log_key_action("返却", "音倉・音練", user_name)
                                reply = f"音倉・音練 を {user_name} さんが返却しました。"
                                logger.info(f"返却操作成功: 音倉・音練 を {user_name} さんが返却しました")
                                asyncio.run(send_line_message(reply_token, reply))
                                asyncio.run(push_to_authenticated_groups(reply))
                                continue

                            # 個別の鍵返却
                            c.execute("SELECT holder_id FROM key_holders WHERE key_name=?", (key_name,))
                            holder = c.fetchone()
                            if holder and holder[0] == user_id:
                                c.execute("DELETE FROM key_holders WHERE key_name=?", (key_name,))
                                conn.commit()
                                user_name = get_user_name(user_id)
                                log_key_action("返却", key_name, user_name)
                                reply = f"{key_name} を {user_name} さんが返却しました。"
                                logger.info(f"返却操作成功: {key_name} を {user_name} さんが返却しました")
                                asyncio.run(send_line_message(reply_token, reply))
                                asyncio.run(push_to_authenticated_groups(reply))
                            else:
                                reply = f"{key_name} は借りられていません、または他のユーザーが所有しています。"
                                logger.warning(f"返却操作失敗: {key_name} が他のユーザー所有")
                                asyncio.run(send_line_message(reply_token, reply))

                        # 引き継ぎ処理
                        elif action == "引き継ぎ":
                            logger.info(f"引き継ぎ操作開始: {key_name}")
                            # 両方引き継ぎの場合、事前に所有者チェック
                            if len(keys_to_process) == 2 and key_name == "音倉":
                                c.execute("SELECT holder_id FROM key_holders WHERE key_name IN (?, ?)", ("音倉", "音練"))
                                holders = c.fetchall()
                                if len(holders) != 2 or holders[0][0] != holders[1][0]:
                                    reply = "音倉と音練は同じ所有者でないため、同時に引き継ぎできません。"
                                    logger.warning(f"引き継ぎ操作失敗: 音倉と音練の所有者が異なる")
                                    asyncio.run(send_line_message(reply_token, reply))
                                    continue

                                # 両方の鍵を一括で引き継ぎ
                                new_name = get_user_name(user_id)
                                c.execute("UPDATE key_holders SET holder_id=?, borrow_time=? WHERE key_name IN (?, ?)",
                                          (user_id, now, "音倉", "音練"))
                                conn.commit()
                                log_key_action("引き継ぎ", "音倉・音練", new_name)
                                reply = f"音倉・音練 を {new_name} さんに引き継ぎました。"
                                logger.info(f"引き継ぎ操作成功: 音倉・音練 を {new_name} さんに引き継ぎました")
                                asyncio.run(send_line_message(reply_token, reply))
                                asyncio.run(push_to_authenticated_groups(reply))
                                continue

                    except Exception as e:
                        error_message = f"操作中にエラーが発生しました: {str(e)}"
                        logger.error(error_message)
                        reply = error_message
                        asyncio.run(send_line_message(reply_token, reply))

            finally:
                if conn:
                    conn.close()
                    logger.info("データベース接続をクローズしました")





        # 鍵確認
        if text == "鍵確認":
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT key_name, holder_id, borrow_time FROM key_holders")
            rows = c.fetchall()
            if not rows:
                reply = "現在、貸出中の鍵はありません。"
            else:
                parts = []
                for key_name, holder_id, borrow_time in rows:
                    holder_name = get_user_name(holder_id)
                    parts.append(f"{key_name} → {holder_name} ({borrow_time})")
                reply = "\n".join(parts)
            conn.close()
            asyncio.run(send_line_message(reply_token, reply))
            continue

        if text == "履歴確認":
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT group_id FROM groups WHERE group_id=?", (group_id,))
            if c.fetchone():
                asyncio.run(send_history(reply_token))
            else:
                reply = "このグループは認証されていません。"
                asyncio.run(send_line_message(reply_token, reply))
            conn.close()
            continue

        if text == "リセット鍵情報":
            # 認証済みグループか確認
            c.execute("SELECT group_id FROM groups WHERE group_id=?", (group_id,))
            if c.fetchone():
                # リセット実行
                try:
                    asyncio.run(human_reset_key_holders())
                    reply = "鍵の保有情報をリセットしました。"
                    logger.info("鍵保有情報リセット実行")
                except Exception as e:
                    reply = f"リセット中にエラーが発生しました: {str(e)}"
                    logger.error(f"リセット失敗: {str(e)}")
            else:
                reply = "このグループは認証されていません。"
                logger.warning("認証されていないグループからリセットコマンドが送信されました。")
            conn.close()
            asyncio.run(send_line_message(reply_token, reply))
            continue


        if text == "履歴削除":
            c.execute("SELECT group_id FROM groups WHERE group_id=?", (group_id,))
            if c.fetchone():
                try:
                    # 30日前の日付を取得
                    cutoff_date = (datetime.now() - timedelta(days=10)).strftime("%Y/%m/%d %H:%M:%S")
                    c.execute("DELETE FROM key_logs WHERE timestamp < ?", (cutoff_date,))
                    conn.commit()
                    reply = "10日以前の履歴を削除しました。"
                    logger.info(f"履歴削除: {cutoff_date} より前の記録を削除しました。")
                except Exception as e:
                    reply = f"履歴削除中にエラーが発生しました: {str(e)}"
                    logger.error(f"履歴削除エラー: {str(e)}")
            else:
                reply = "このグループは認証されていません。"
                logger.warning("認証されていないグループから履歴削除が送信されました。")

            conn.close()
            asyncio.run(send_line_message(reply_token, reply))
            continue


    return jsonify({"status": "ok"})

#鍵管理処理内での名前取得
def get_user_name(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name FROM users WHERE line_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        return result[0]
    return "不明なユーザー"

#認証済みのグループへのメッセージ送信
async def push_to_authenticated_groups(message: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT group_id FROM groups")
    groups = c.fetchall()
    conn.close()

    for group in groups:
        group_id = group[0]
        await push_line_message(group_id, message)

#ユーザー登録確認
def is_user_registered(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE line_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

#鍵のリセット処理
async def reset_key_holders():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # 鍵保有情報を全て削除
        c.execute("DELETE FROM key_holders")
        conn.commit()
        conn.close()

        # リセット完了通知をグループに送信
        message = "24時を過ぎました。(若しくは手動操作により)本日の鍵保有情報をリセットしました。"
        await push_to_authenticated_groups(message)
        logger.info("24時リセット完了(若しくは手動リセット完了)全ての鍵保有情報を削除しました。")
    except Exception as e:
        logger.error(f"リセット処理でエラーが発生しました: {str(e)}")


async def human_reset_key_holders():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # 鍵保有情報を全て削除
        c.execute("DELETE FROM key_holders")
        conn.commit()
        conn.close()

        logger.info("手動操作により全ての鍵保有情報を削除しました。")
    except Exception as e:
        logger.error(f"リセット処理でエラーが発生しました: {str(e)}")





# Flask実行
if __name__ == "__main__":
    try:
        # スケジューラを非同期で開始
        start_scheduler()
        logger.info("Flask and APScheduler starting...")
        app.run(host="127.0.0.1", port=5050, debug=False)
    except Exception as e:
        logger.error(f"Error starting Flask app: {str(e)}")



