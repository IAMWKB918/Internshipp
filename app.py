#!/usr/bin/env python3
"""
app.py — 给 main.py 那条 pipeline 套一层网页界面。

跑法:
    pip install flask
    python app.py
    然后浏览器打开 http://127.0.0.1:5000

页面逻辑 (templates/florence.html) 是先把整批图片一次性交给后端处理完，
拿到全部结果后再在前端「回放」扫描动画 —— 不是真正的实时推流，
这个跟你原来那份 sample 的设计是一致的，只是这次数据是真的 pipeline 结果。
"""

import json
import os
import shutil
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from main import (
    SCRIPT_DIR,
    DEFAULT_CONFIG,
    DEFAULT_OUTPUT,
    run_pipeline,
    PipelineError,
)

app = Flask(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

OUTPUT_DIR = DEFAULT_OUTPUT          # .../florence/output
CONFIG_PATH = DEFAULT_CONFIG         # .../florence/config.json
BATCH_INPUT_DIR = OUTPUT_DIR / "_batch_input"   # 每次跑之前先清空重建
UPLOAD_TMP_DIR = SCRIPT_DIR / "_uploads_temp"   # "Add Files" 上传的文件先落这里


# ────────────────────────────────────────────────────────────────
# 页面
# ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("florence.html")


# ────────────────────────────────────────────────────────────────
# 选文件夹 (弹原生的 Windows 文件夹选择框)
# ────────────────────────────────────────────────────────────────

@app.route("/select_folder", methods=["POST"])
def select_folder():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory()
    root.destroy()

    return jsonify({"path": path or None})


# ────────────────────────────────────────────────────────────────
# 上传单个/多个文件 (浏览器 <input type=file multiple> 那个按钮)
# ────────────────────────────────────────────────────────────────

@app.route("/upload_temp", methods=["POST"])
def upload_temp():
    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in request.files.getlist("files[]"):
        if not f.filename:
            continue
        dest = UPLOAD_TMP_DIR / f.filename
        f.save(str(dest))
        paths.append(str(dest))
    return jsonify({"paths": paths})


# ────────────────────────────────────────────────────────────────
# 真正跑整条 pipeline
# ────────────────────────────────────────────────────────────────

@app.route("/run_tasks", methods=["POST"])
def run_tasks():
    data = request.get_json(force=True) or {}
    tasks = data.get("tasks", [])
    if not tasks:
        return jsonify({"error": "No tasks provided"}), 400

    # ---- 1. 把这一批任务里所有图片汇总复制到同一个临时输入文件夹 ----
    if BATCH_INPUT_DIR.exists():
        shutil.rmtree(BATCH_INPUT_DIR)
    BATCH_INPUT_DIR.mkdir(parents=True)

    for t in tasks:
        src = Path(t.get("path", ""))
        if t.get("type") == "folder":
            if src.is_dir():
                for f in sorted(src.iterdir()):
                    if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                        shutil.copy2(f, BATCH_INPUT_DIR / f.name)
        else:  # 单个文件 (已经在 /upload_temp 落过盘了)
            if src.is_file() and src.suffix.lower() in IMAGE_EXTS:
                shutil.copy2(src, BATCH_INPUT_DIR / src.name)

    image_files = sorted(f for f in BATCH_INPUT_DIR.iterdir() if f.is_file())
    if not image_files:
        return jsonify({"error": "选中的文件夹/文件里没有找到可识别的图片"}), 400

    # ---- 2. 真正跑 florence -> exif -> paddleocr -> mixjson -> classifier -> organizer ----
    try:
        run_pipeline(BATCH_INPUT_DIR, OUTPUT_DIR, CONFIG_PATH)
    except PipelineError as e:
        return jsonify({"error": f"{e.step_name} 失败 (退出码 {e.returncode})，详情看服务器终端的日志"}), 500

    # ---- 3. 从 classifier 产出的 manifest 里读每张图分到哪个 年份/分类 ----
    manifest_path = OUTPUT_DIR / "sorted" / "classify_manifest.json"
    manifest = []
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    logs = []
    for entry in manifest:
        file_stem = str(entry.get("file", ""))
        match = next((f for f in image_files if f.stem.lower() == file_stem.lower()), None)
        logs.append({
            "file": match.name if match else file_stem,
            "thumb_url": f"/thumb/{match.name}" if match else "",
            "result": f"{entry.get('year', '?')} / {entry.get('category', '?')}",
        })

    # ---- 4. 汇总最终 organized_photos 里 年份 -> 分类 -> 数量 ----
    organized_dir = OUTPUT_DIR / "organized_photos"
    summary = {}
    if organized_dir.exists():
        for year_dir in sorted(p for p in organized_dir.iterdir() if p.is_dir()):
            categories = {}
            for cat_dir in sorted(p for p in year_dir.iterdir() if p.is_dir()):
                count = len([f for f in cat_dir.iterdir() if f.is_file()])
                categories[cat_dir.name] = count
            if categories:
                summary[year_dir.name] = categories

    return jsonify({
        "logs": logs,
        "summary": summary,
        "output_path": str(organized_dir),
    })


# ────────────────────────────────────────────────────────────────
# 给扫描动画用的缩略图 (直接读这一批任务的临时输入文件夹)
# ────────────────────────────────────────────────────────────────

@app.route("/thumb/<path:filename>")
def thumb(filename):
    return send_from_directory(str(BATCH_INPUT_DIR), filename)


# ────────────────────────────────────────────────────────────────
# 在系统文件管理器里打开某个年份/分类文件夹 (或 root = 整个 organized_photos)
# ────────────────────────────────────────────────────────────────

@app.route("/open_folder/<path:target>")
def open_folder(target):
    organized_dir = OUTPUT_DIR / "organized_photos"
    folder = organized_dir if target == "root" else organized_dir / target

    if not folder.exists():
        return jsonify({"ok": False, "error": "folder not found"}), 404

    if sys.platform == "win32":
        os.startfile(str(folder))
    elif sys.platform == "darwin":
        os.system(f'open "{folder}"')
    else:
        os.system(f'xdg-open "{folder}"')

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)