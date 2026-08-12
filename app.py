#!/usr/bin/env python3
"""
app.py — 给 main.py 那条 pipeline 套一层网页界面。

跑法:
    pip install flask
    python app.py
    然后浏览器打开 http://127.0.0.1:5001

页面逻辑 (templates/florence.html) 是先把整批图片一次性交给后端处理完，
拿到全部结果后再在前端「回放」扫描动画 —— 不是真正的实时推流，
这个跟你原来那份 sample 的设计是一致的，只是这次数据是真的 pipeline 结果。

Input / Output 规则 —「各回各家」:
- 选了几个资料夹，就各自完整跑一次 pipeline，各自的 output 长在自己
  资料夹底下的 "output" 子资料夹，完全不复制、不合并图片。
- 用「Add Files」上传的零散文件本来就没有自己的资料夹，会落在
  UPLOAD_TMP_DIR 里，当成"一个批次"单独跑一次，output 长在
  UPLOAD_TMP_DIR/output。
- 缺点：选几个来源，Florence 模型就要重新载入几次，比合并成一批跑慢；
  这是换取「不产生额外备份复制」的取舍。
"""

import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from main import (
    SCRIPT_DIR,
    DEFAULT_CONFIG,
    run_pipeline,
    PipelineError,
)

app = Flask(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

CONFIG_PATH = DEFAULT_CONFIG                    # .../florence/config.json
UPLOAD_TMP_DIR = SCRIPT_DIR / "_uploads_temp"    # "Add Files" 上传的文件先落这里

# 记录最近一次 /run_tasks 里，每个批次实际用的 input/output，给 /thumb、/open_folder 用
STATE = {"runs": []}  # [{"input_dir": Path, "output_dir": Path}, ...]


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
# 真正跑整条 pipeline —— 每个来源各自独立跑一次
# ────────────────────────────────────────────────────────────────

def _batches_from_tasks(tasks):
    """
    把前端传来的 tasks 拆成一个个独立批次：
    - 每个 folder task 各自是一个批次，input 就是那个资料夹本身。
    - 所有零散上传的 file task 合成一个批次，input 是 UPLOAD_TMP_DIR
      (文件已经在 /upload_temp 落盘了，不用再复制一次)。
    返回 [(input_dir, image_files), ...]
    """
    batches = []

    folder_tasks = [t for t in tasks if t.get("type") == "folder"]
    file_tasks = [t for t in tasks if t.get("type") != "folder"]

    for t in folder_tasks:
        src = Path(t.get("path", ""))
        if not src.is_dir():
            continue
        image_files = sorted(
            f for f in src.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS
        )
        if image_files:
            batches.append((src, image_files))

    if file_tasks:
        image_files = []
        for t in file_tasks:
            src = Path(t.get("path", ""))
            if src.is_file() and src.suffix.lower() in IMAGE_EXTS:
                image_files.append(src)
        if image_files:
            batches.append((UPLOAD_TMP_DIR, sorted(image_files)))

    return batches


@app.route("/run_tasks", methods=["POST"])
def run_tasks():
    data = request.get_json(force=True) or {}
    tasks = data.get("tasks", [])
    if not tasks:
        return jsonify({"error": "No tasks provided"}), 400

    batches = _batches_from_tasks(tasks)
    if not batches:
        return jsonify({"error": "选中的文件夹/文件里没有找到可识别的图片"}), 400

    STATE["runs"] = []
    all_logs = []
    batch_results = []  # 每个来源各自一个区块，前端分开显示、分开开资料夹
    errors = []

    for batch_index, (input_dir, image_files) in enumerate(batches):
        output_dir = input_dir / "output"  # output 长在各自 input 资料夹底下
        STATE["runs"].append({"input_dir": input_dir, "output_dir": output_dir})

        try:
            run_pipeline(input_dir, output_dir, CONFIG_PATH)
        except PipelineError as e:
            errors.append(f"[{input_dir.name}] {e.step_name} 失败 (退出码 {e.returncode})")
            continue  # 这批失败了，继续跑下一批，不整个中断

        # ---- 从 classifier 产出的 manifest 里读每张图分到哪个 年份/分类 ----
        manifest_path = output_dir / "classify_manifest.json"
        manifest = []
        if manifest_path.exists():
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)

        for entry in manifest:
            file_stem = str(entry.get("file", ""))
            match = next((f for f in image_files if f.stem.lower() == file_stem.lower()), None)
            all_logs.append({
                "file": match.name if match else file_stem,
                "thumb_url": f"/thumb/{batch_index}/{match.name}" if match else "",
                "result": f"{entry.get('year', '?')} / {entry.get('category', '?')}",
            })

        # ---- 这一批自己的 organized_photos 汇总 (不跟其他批次混在一起) ----
        organized_dir = output_dir / "organized_photos"
        batch_summary = {}
        if organized_dir.exists():
            for year_dir in sorted(p for p in organized_dir.iterdir() if p.is_dir()):
                categories = {}
                for cat_dir in sorted(p for p in year_dir.iterdir() if p.is_dir()):
                    categories[cat_dir.name] = len([f for f in cat_dir.iterdir() if f.is_file()])
                if categories:
                    batch_summary[year_dir.name] = categories

        batch_results.append({
            "batch_index": batch_index,
            "name": input_dir.name,
            "output_path": str(organized_dir),
            "summary": batch_summary,
        })

    response = {
        "logs": all_logs,
        "batches": batch_results,
    }
    if errors:
        response["errors"] = errors
    return jsonify(response)


# ────────────────────────────────────────────────────────────────
# 给扫描动画用的缩略图 —— 按批次索引找回对应 input_dir
# ────────────────────────────────────────────────────────────────

@app.route("/thumb/<int:batch_index>/<path:filename>")
def thumb(batch_index, filename):
    if batch_index >= len(STATE["runs"]):
        return jsonify({"error": "batch not found"}), 404
    return send_from_directory(str(STATE["runs"][batch_index]["input_dir"]), filename)


# ────────────────────────────────────────────────────────────────
# 在系统文件管理器里打开某个批次的输出资料夹 (batch_index 对应 STATE["runs"] 顺序，
# 或直接给某一批 organized_photos 底下的年份/分类子路径)
# ────────────────────────────────────────────────────────────────

@app.route("/open_folder/<int:batch_index>")
@app.route("/open_folder/<int:batch_index>/<path:target>")
def open_folder(batch_index, target="root"):
    if batch_index >= len(STATE["runs"]):
        return jsonify({"ok": False, "error": "batch not found"}), 404

    organized_dir = STATE["runs"][batch_index]["output_dir"] / "organized_photos"
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
    app.run(host='127.0.0.1', port=5001, debug=True, use_reloader=False)