import os
import sqlite3
import numpy as np
from flask import Flask, request, jsonify
from PIL import Image
from datetime import datetime
from uuid import uuid4

app = Flask(__name__)
DB_PATH = "generation.db"
IMG_DIR = "./images"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_selection (
        user_id TEXT,
        generation INTEGER,
        image1 TEXT,
        image2 TEXT,
        selected INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def get_current_generation(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT MAX(generation) FROM user_selection WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return (row[0] + 1) if row and row[0] is not None else 0

def generate_random_image(path, size=(512, 512)):
    img_array = np.random.randint(0, 256, (size[1], size[0], 3), dtype=np.uint8)
    img = Image.fromarray(img_array)
    img.save(path)

def mutate_image(image_path, save_path, mutation_rate=0.01):
    img = Image.open(image_path)
    arr = np.array(img)
    mutation_mask = np.random.rand(*arr.shape) < mutation_rate
    arr[mutation_mask] = np.random.randint(0, 256, size=arr[mutation_mask].shape, dtype=np.uint8)
    mutated_img = Image.fromarray(arr.astype(np.uint8))
    mutated_img.save(save_path)

@app.route("/generate", methods=["POST"])
def generate():
    user_id = request.json.get("user_id")
    generation = get_current_generation(user_id)
    user_folder = os.path.join(IMG_DIR, user_id, str(generation))
    os.makedirs(user_folder, exist_ok=True)

    if generation == 0:
        img1_path = os.path.join(user_folder, "1.jpg")
        img2_path = os.path.join(user_folder, "2.jpg")
        generate_random_image(img1_path)
        generate_random_image(img2_path)
    else:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT selected FROM user_selection WHERE user_id=? AND generation=?", (user_id, generation - 1))
        row = c.fetchone()
        conn.close()

        if row:
            selected = row[0]
            parent_path = os.path.join(IMG_DIR, user_id, str(generation - 1), f"{selected}.jpg")
            mutate_image(parent_path, os.path.join(user_folder, "1.jpg"))
            mutate_image(parent_path, os.path.join(user_folder, "2.jpg"))
        else:
            generate_random_image(os.path.join(user_folder, "1.jpg"))
            generate_random_image(os.path.join(user_folder, "2.jpg"))

    return jsonify({
        "image1": f"https://photo.minepus.net/images/{user_id}/{generation}/1.jpg",
        "image2": f"https://photo.minepus.net/images/{user_id}/{generation}/2.jpg",
        "generation": generation
    })

@app.route("/select", methods=["POST"])
def select():
    data = request.json
    user_id = data["user_id"]
    generation = data["generation"]
    selected = data["selected"]
    image1 = data["img1"]
    image2 = data["img2"]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO user_selection (user_id, generation, image1, image2, selected)
                 VALUES (?, ?, ?, ?, ?)''', (user_id, generation, image1, image2, selected))
    conn.commit()
    conn.close()

    return jsonify({"status": "success"})

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
