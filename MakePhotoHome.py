import os
import json
import random
import numpy as np
from flask import Flask, request, jsonify, send_file
from PIL import Image
import logging
from flask import send_from_directory
import sqlite3

# ログ設定
logging.basicConfig(filename='photo_server.log', level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')

app = Flask(__name__)

# 写真保存フォルダ
BASE_DIR = "images"
os.makedirs(BASE_DIR, exist_ok=True)

DB_PATH = "ga_state.db"

def init_db():#DB初期化
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS ga_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        generation INTEGER
    )''')
    c.execute('INSERT OR IGNORE INTO ga_state (id, generation) VALUES (1, 0)')
    conn.commit()
    conn.close()

def get_generation():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT generation FROM ga_state WHERE id = 1')
    generation = c.fetchone()[0]
    conn.close()
    return generation

def set_generation(gen):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE ga_state SET generation = ? WHERE id = 1', (gen,))
    conn.commit()
    conn.close()



# 全体状態共有
global_state = {
    "selected": None,
    "is_locked": False
}

def generate_image_array():#写真生成
    return np.random.randint(0, 256, (512, 512, 3), dtype=np.uint8)

def save_image(path, array):#写真保存
    Image.fromarray(array).save(path, format="JPEG")

def evolve(parent1, parent2):
    mask = np.random.rand(*parent1.shape) < 0.5
    return np.where(mask, parent1, parent2)

@app.route("/generate", methods=["POST"])#写真生成受付
def generate():
    global global_state
    try:
        if global_state["is_locked"]:
            logging.info("Generation locked: waiting for previous selection.")
            return jsonify({"error": "locked"}), 403

        user_id = request.json.get("user_id", "anonymous")
        generation = get_generation()
        user_folder = os.path.join(BASE_DIR, str(generation))
        os.makedirs(user_folder, exist_ok=True)

        logging.info(f"{user_id} - {generation} image generation started")

        if generation == 0:
            img1 = generate_image_array()
            img2 = generate_image_array()
        else:
            selected = global_state["selected"]
            prev_folder = os.path.join(BASE_DIR, str(generation - 1))
            parent_path = os.path.join(prev_folder, f"{selected}.jpg")
            parent_img = np.array(Image.open(parent_path))
            other_img = generate_image_array()
            img1 = evolve(parent_img, other_img)
            img2 = evolve(other_img, parent_img)

        save_image(os.path.join(user_folder, "1.jpg"), img1)
        save_image(os.path.join(user_folder, "2.jpg"), img2)

        global_state["selected"] = None
        global_state["is_locked"] = True

        res = {
            "image1": f"http://10.0.0.1:5000/images/{generation}/1.jpg",
            "image2": f"http://10.0.0.1:5000/images/{generation}/2.jpg",
            "generation": generation
        }

        logging.info(f"User {user_id} - Global Generation {generation} images generated")

        return jsonify(res)
    except Exception as e:
        logging.error(f"Image generation error: {e}", exc_info=True)
        return jsonify({"error": "generation_failed"}), 500


@app.route("/select", methods=["POST"])
def select():
    global global_state
    try:
        selected = int(request.json["selected"])
        global_state["selected"] = selected
        global_state["is_locked"] = False

        current_gen = get_generation()
        new_gen = current_gen + 1
        set_generation(new_gen)

        logging.info(f"Selection made: image {selected}, moving to generation {new_gen}")
        return jsonify({"status": "ok"})
    except Exception as e:
        logging.error(f"Selection error: {e}", exc_info=True)
        return jsonify({"error": "selection_failed"}), 500

@app.route("/images/<user_id>/<gen>/<img>", methods=["GET"])
def serve_image(user_id, gen, img):
    try:
        path = os.path.join(BASE_DIR, gen, img)
        if not os.path.exists(path):
            logging.warning(f"Image not found: {path}")
            return "Not Found", 404
        return send_file(path, mimetype="image/jpeg")
    except Exception as e:
        logging.error(f"Serving image error: {e}", exc_info=True)
        return "Error", 500

@app.route("/reset", methods=["POST"])
def reset():
    global global_state
    try:
        set_generation(0)
        global_state = {"selected": None, "is_locked": False}
        logging.info("Global reset completed.")
        return jsonify({"status": "reset"})
    except Exception as e:
        logging.error(f"Reset error: {e}", exc_info=True)
        return jsonify({"error": "reset_failed"}), 500

@app.route("/status")
def check_status():
    generation = request.args.get("gen")
    if not generation:
        return jsonify({"error": "Missing generation"}), 400

    gen_path = os.path.join("images", generation)
    img1 = os.path.join(gen_path, "1.jpg")
    img2 = os.path.join(gen_path, "2.jpg")

    exists = os.path.exists(img1) and os.path.exists(img2)
    return jsonify({"ready": exists}), 200

@app.route('/images/<int:gen>/<filename>')
def serve_generated_image(gen, filename):
    folder = os.path.join("images", str(gen))
    return send_from_directory(folder, filename)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)

