import json
import os
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from main import (
    SCRIPT_DIR,
    DEFAULT_CONFIG,
    run_pipeline,
    PipelineError,
)
from auto_cmsw import run_folder_name_task

app = Flask(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

CONFIG_PATH = DEFAULT_CONFIG                    # .../florence/config.json
UPLOAD_TMP_DIR = SCRIPT_DIR / "_uploads_temp"    # "Add Files" 上传的文件先落这里


def load_media_exts(config_path=CONFIG_PATH):
    """图片副档名 + config.json 里的 video_formats，合并成"这个批次里
    该算进总数的文件类型"清单。之前这里只认 IMAGE_EXTS，代表选中的资料夹
    里如果混了 .mp4/.mp3，这些文件从一开始扫资料夹那一步就被漏掉了 ——
    连 total 计数都不会算到它们，更别提后面的 classified/missing 统计。"""
    exts = set(IMAGE_EXTS)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for fmt in cfg.get("video_formats", []):
            exts.add("." + fmt.lower().lstrip("."))
    except Exception as e:
        print(f"[warn] 无法从 {config_path} 读取 video_formats: {e}")
    return exts


# 记录最近一次 /run_tasks 里，每个批次实际用的 input/output，给 /open_folder 用
# progress: 背景执行绪一边跑一边写入这里，/progress 路由读出来给前端轮询。
PROGRESS_LOCK = threading.Lock()
STATE = {
    "runs": [],       # [{"input_dir": Path, "output_dir": Path}, ...]
    "progress": {      # 当前 (或最近一次) run 的即时状态，给 /progress 轮询用
        "run_id": None,
        "active": False,
        "finished": True,
        "batches": [],  # 每个批次一份 dict，随处理进度原地更新
        "errors": [],
    },
}


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
# 真正跑整条 pipeline —— 每个来源各自独立跑一次，背景执行绪进行
# ────────────────────────────────────────────────────────────────

def _batches_from_tasks(tasks, media_exts):
    """
    把前端传来的 tasks 拆成一个个独立批次：
    - 每个 folder task 各自是一个批次，input 就是那个资料夹本身。
    - 所有零散上传的 file task 合成一个批次，input 是 UPLOAD_TMP_DIR
      (文件已经在 /upload_temp 落盘了，不用再复制一次)。
    返回 [(input_dir, media_files), ...]  —— media_files 包含图片 + video/audio。
    """
    batches = []

    folder_tasks = [t for t in tasks if t.get("type") == "folder"]
    file_tasks = [t for t in tasks if t.get("type") != "folder"]

    for t in folder_tasks:
        src = Path(t.get("path", ""))
        if not src.is_dir():
            continue
        media_files = sorted(
            f for f in src.iterdir()
            if f.is_file() and f.suffix.lower() in media_exts
        )
        if media_files:
            batches.append((src, media_files))

    if file_tasks:
        media_files = []
        for t in file_tasks:
            src = Path(t.get("path", ""))
            if src.is_file() and src.suffix.lower() in media_exts:
                media_files.append(src)
        if media_files:
            batches.append((UPLOAD_TMP_DIR, sorted(media_files)))

    return batches


def _compute_batch_result(batch_index, output_dir, media_files):
    """跑完一个批次的 pipeline 后，读 manifest + organized_photos 算出
    分类分布、total/classified/missing，外加逐张图片的 log(给扫描动画用)。
    抽成独立函式，方便背景执行绪调用。"""
    manifest_path = output_dir / "classify_manifest.json"
    manifest = []
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    manifest_by_stem = {str(e.get("file", "")).lower(): e for e in manifest}

    # organizer.py 会把每张图的处理结果写进 entry["status"]：
    # "copied to ..." / "symlinked to ..." / "dry-run planned: ..." 代表成功，
    # "source image not found, skipped" 代表源文件没找到。
    classified_count = 0
    missing_files = []
    for f in media_files:
        entry = manifest_by_stem.get(f.stem.lower())
        status = str((entry or {}).get("status", ""))
        ok = status.startswith(("copied", "symlinked", "dry-run"))
        if entry and ok:
            classified_count += 1
        else:
            missing_files.append(f.name)

    # 逐张图片的分类结果，给前端扫描动画用（每个 batch 各自的一份 log）；
    # video/audio 没有缩略图，thumb_url 留空，前端会跳过设图只显示文件名。
    logs = []
    for entry in manifest:
        file_stem = str(entry.get("file", ""))
        match = next((f for f in media_files if f.stem.lower() == file_stem.lower()), None)
        is_image = bool(match) and match.suffix.lower() in IMAGE_EXTS
        logs.append({
            "file": match.name if match else file_stem,
            "thumb_url": f"/thumb/{batch_index}/{match.name}" if is_image else "",
            "result": entry.get("category", "?"),
        })

    # organizer.py 直接把文件放在 organized_photos/{category}/ 底下，
    # 没有 year 这一层子资料夹。
    organized_dir = output_dir / "organized_photos"
    batch_summary = {}
    if organized_dir.exists():
        for cat_dir in sorted(p for p in organized_dir.iterdir() if p.is_dir()):
            count = len([f for f in cat_dir.iterdir() if f.is_file()])
            if count:
                batch_summary[cat_dir.name] = count

    total = len(media_files)
    return {
        "output_path": str(organized_dir),
        "summary": batch_summary,
        "total": total,
        "classified": classified_count,
        "missing": total - classified_count,
        "missing_files": missing_files,
        "logs": logs,
    }


def _run_batches_background(run_id, batches):
    """在背景执行绪里依序跑每个批次的 pipeline，每跑完一个就立刻把结果写进
    STATE["progress"]，让 /progress 轮询能马上看到 —— 不用等全部批次跑完。"""
    for batch_index, (input_dir, media_files) in enumerate(batches):
        with PROGRESS_LOCK:
            if STATE["progress"]["run_id"] != run_id:
                return  # 被新的一次 run 取代了，这个旧执行绪直接放弃
            STATE["progress"]["batches"][batch_index]["status"] = "running"
            STATE["progress"]["batches"][batch_index]["start_time"] = time.time()

        output_dir = input_dir / "output"
        with PROGRESS_LOCK:
            STATE["runs"].append({"input_dir": input_dir, "output_dir": output_dir})

        start_time = STATE["progress"]["batches"][batch_index]["start_time"]

        # ────────────────────────────────────────────────────────
        # 两个互不相干的工作，同时启动：
        #   1) run_pipeline      —— main.py 的 Florence/YOLO/分类/归档 主流程
        #   2) run_folder_name_task —— auto_cmsw.py，只抓 folder name 文字写 txt
        # 用两条 thread 一起 start/join，达到"同时进行"而不是先后顺序执行。
        # 两边互不 import，出错也互不拖累对方。
        # ────────────────────────────────────────────────────────
        batch_errors = {}

        def _do_pipeline():
            try:
                run_pipeline(input_dir, output_dir, CONFIG_PATH)
            except PipelineError as e:
                batch_errors["pipeline"] = e

        def _do_folder_name():
            try:
                run_folder_name_task(input_dir, output_dir)
            except Exception as e:
                batch_errors["folder_name"] = e

        t_pipeline = threading.Thread(target=_do_pipeline)
        t_name = threading.Thread(target=_do_folder_name)
        t_pipeline.start()
        t_name.start()
        t_pipeline.join()
        t_name.join()

        if "pipeline" in batch_errors:
            e = batch_errors["pipeline"]
            elapsed = round(time.time() - start_time, 1)
            msg = f"{e.step_name} 失败 (退出码 {e.returncode})"
            with PROGRESS_LOCK:
                if STATE["progress"]["run_id"] != run_id:
                    return
                b = STATE["progress"]["batches"][batch_index]
                b["status"] = "error"
                b["elapsed_seconds"] = elapsed
                b["error_message"] = msg
                STATE["progress"]["errors"].append(f"[{input_dir.name}] {msg}")
            continue  # 这批失败了，继续跑下一批，不整个中断

        if "folder_name" in batch_errors:
            # 这一步失败不算整个 batch 失败，只是少一个 txt，记 log 就好
            print(f"[warn] [{input_dir.name}] auto_cmsw 抓取失败: {batch_errors['folder_name']}")

        result = _compute_batch_result(batch_index, output_dir, media_files)
        elapsed = round(time.time() - start_time, 1)

        with PROGRESS_LOCK:
            if STATE["progress"]["run_id"] != run_id:
                return
            b = STATE["progress"]["batches"][batch_index]
            b["status"] = "done"
            b["elapsed_seconds"] = elapsed
            b.update(result)

    with PROGRESS_LOCK:
        if STATE["progress"]["run_id"] == run_id:
            STATE["progress"]["active"] = False
            STATE["progress"]["finished"] = True


@app.route("/run_tasks", methods=["POST"])
def run_tasks():
    data = request.get_json(force=True) or {}
    tasks = data.get("tasks", [])
    if not tasks:
        return jsonify({"error": "No tasks provided"}), 400

    media_exts = load_media_exts(CONFIG_PATH)
    batches = _batches_from_tasks(tasks, media_exts)
    if not batches:
        return jsonify({"error": "选中的文件夹/文件里没有找到可识别的图片或视频"}), 400

    run_id = time.time()
    initial_batches = [
        {
            "batch_index": idx,
            "name": input_dir.name,
            "status": "pending",
            "start_time": None,
            "elapsed_seconds": 0,
            "output_path": None,
            "summary": {},
            "total": len(media_files),
            "classified": 0,
            "missing": 0,
            "missing_files": [],
            "logs": [],
            "error_message": None,
        }
        for idx, (input_dir, media_files) in enumerate(batches)
    ]

    with PROGRESS_LOCK:
        STATE["runs"] = []
        STATE["progress"] = {
            "run_id": run_id,
            "active": True,
            "finished": False,
            "batches": initial_batches,
            "errors": [],
        }

    thread = threading.Thread(target=_run_batches_background, args=(run_id, batches), daemon=True)
    thread.start()

    return jsonify({"ok": True, "run_id": run_id, "total_batches": len(batches)})


# ────────────────────────────────────────────────────────────────
# 前端轮询进度用：目前 (或最近一次) run 里每个批次的即时状态
# ────────────────────────────────────────────────────────────────

@app.route("/progress")
def progress():
    now = time.time()
    with PROGRESS_LOCK:
        snapshot = {
            "run_id": STATE["progress"]["run_id"],
            "active": STATE["progress"]["active"],
            "finished": STATE["progress"]["finished"],
            "errors": list(STATE["progress"]["errors"]),
            "batches": [],
        }
        for b in STATE["progress"]["batches"]:
            entry = dict(b)
            # running 中的批次，elapsed 要即时算，不然轮询画面上的秒数不会跳动
            if entry["status"] == "running" and entry.get("start_time"):
                entry["elapsed_seconds"] = round(now - entry["start_time"], 1)
            entry.pop("start_time", None)  # 内部用的 epoch 时间戳，不用传给前端
            snapshot["batches"].append(entry)
    return jsonify(snapshot)


@app.route("/thumb/<int:batch_index>/<path:filename>")
def thumb(batch_index, filename):
    if batch_index >= len(STATE["runs"]):
        return jsonify({"error": "batch not found"}), 404
    return send_from_directory(str(STATE["runs"][batch_index]["input_dir"]), filename)


# ────────────────────────────────────────────────────────────────
# 在系统文件管理器里打开某个批次的输出资料夹
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
    # threaded=True: /progress 轮询需要在背景 pipeline 执行绪还在跑的时候
    # 也能被同时处理，不然网页会卡住看不到进度。
    app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False, threaded=True)