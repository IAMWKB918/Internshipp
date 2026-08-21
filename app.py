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
    PipelineStopped,
)
from auto_cmsw import run_folder_analysis
from auto_report import extract_links_from_text, generate_from_links

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
    # 每个 run_id 各自一个 threading.Event。/stop_run 只是把当前 run_id
    # 对应的 event set()，background 线程 (main.py 的 run_step) 轮询到之后
    # 才会真的去砍子程式 —— 之前完全没有这个东西，"Stop" 只是前端 JS
    # 换个按钮文字，后端的 subprocess.run() 照样跑到底。
    "stop_event": None,
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


def _run_cmsw_task(run_id, batch_index, name, input_dir, output_dir, cmsw_start):
    """独立跑某一个 batch 的 auto_cmsw 分析 (分类 + 搜索 + 过滤)。所有 batch 的这个
    函式会在 run 一开始就同时丢进线程池 —— 不排队、不等 pipeline，谁先跑完就先把
    结果写回 STATE，右侧面板马上看得到。"""
    try:
        cmsw_result = run_folder_analysis(input_dir, output_dir)
        cmsw_status = "error" if isinstance(cmsw_result, dict) and cmsw_result.get("error") else "done"
    except Exception as e:
        print(f"[warn] [{name}] auto_cmsw 分析失败: {e}")
        cmsw_result = {"error": str(e)}
        cmsw_status = "error"
    with PROGRESS_LOCK:
        if STATE["progress"]["run_id"] != run_id:
            return
        b = STATE["progress"]["batches"][batch_index]
        b["cmsw"] = cmsw_result
        b["cmsw_status"] = cmsw_status
        b["cmsw_elapsed_seconds"] = round(time.time() - cmsw_start, 1)
        # run_folder_analysis(input_dir, output_dir) 拿到的 output_dir 就是它落盘的地方，
        # 存起来给 /open_cmsw_folder 用，前端才能有个按钮直接打开这个位置
        b["cmsw_output_path"] = str(output_dir)


def _run_batches_background(run_id, batches, stop_event, skip_classify):
    """在背景执行绪里跑所有批次。两条线完全各走各的，互不排队等待：
      - pipeline (图片分类/归档)：还是一个 folder 接一个 folder 顺序处理。
      - auto_cmsw (分类+搜索+过滤)：不跟 pipeline 排队 —— run 一开始就把每个
        folder 的 cmsw 全部同时丢出去背景线程，谁先跑完就先显示，不用等
        "轮到" 第二个 folder 开始处理图片，cmsw 才跟着动。

    skip_classify: 前端 Stop 按钮在按下 Start 之前的状态 (对应 /run_tasks
    body 里的 "stopped")。True 的话，这次 run 完全不叫 run_pipeline —— 不会
    启动 florence.py 去载入模型，也不会跑 YOLO/classifier/organizer，每个
    batch 直接标成 "skipped"；右边的 auto_cmsw (Google Search) 不受影响，
    照样跑。这是「这次要不要跑 classify」的一次性开关，不是跑到一半再中途
    喊停 —— 后者交给 stop_event 处理 (见下面 run_pipeline 调用)。
    """
    # 预先把每个 batch 的 input/output 路径记好 (不用等 pipeline 真的跑到那一批)，
    # 这样 cmsw 就算跑得比 pipeline 快很多，/open_cmsw_folder、/thumb 也不会因为
    # STATE["runs"] 还没建好而 404。
    with PROGRESS_LOCK:
        if STATE["progress"]["run_id"] != run_id:
            return
        STATE["runs"] = [
            {"input_dir": input_dir, "output_dir": input_dir / "output"}
            for input_dir, _ in batches
        ]

    # ---- 所有 batch 的 cmsw 一次性同时启动，互不排队 ----
    cmsw_threads = []
    for batch_index, (input_dir, _media_files) in enumerate(batches):
        output_dir = input_dir / "output"
        cmsw_start = time.time()
        with PROGRESS_LOCK:
            if STATE["progress"]["run_id"] != run_id:
                return
            b = STATE["progress"]["batches"][batch_index]
            b["cmsw_status"] = "running"
            b["cmsw_start_time"] = cmsw_start

        t = threading.Thread(
            target=_run_cmsw_task,
            args=(run_id, batch_index, input_dir.name, input_dir, output_dir, cmsw_start),
            daemon=True,
        )
        t.start()
        cmsw_threads.append(t)

    # ---- pipeline (图片分类/归档) 依然一个 folder 一个 folder 顺序处理 ----
    for batch_index, (input_dir, media_files) in enumerate(batches):
        # Stop 在按 Start 之前就已经被按下：这次 run 直接跳过 classify，
        # 连 florence 模型都不会去载入。
        if skip_classify:
            with PROGRESS_LOCK:
                if STATE["progress"]["run_id"] != run_id:
                    return
                b = STATE["progress"]["batches"][batch_index]
                b["status"] = "skipped"
            continue

        with PROGRESS_LOCK:
            if STATE["progress"]["run_id"] != run_id:
                return  # 被新的一次 run 取代了，这个旧执行绪直接放弃
            STATE["progress"]["batches"][batch_index]["status"] = "running"
            STATE["progress"]["batches"][batch_index]["start_time"] = time.time()

        output_dir = input_dir / "output"
        start_time = STATE["progress"]["batches"][batch_index]["start_time"]

        try:
            run_pipeline(input_dir, output_dir, CONFIG_PATH, stop_event=stop_event)
        except PipelineStopped as e:
            # 使用者主动按 Stop 中止的，跟真的跑失败(PipelineError)分开处理，
            # 不算错误、不进 errors 列表，前端可以显示成「已停止」而不是红字报错。
            elapsed = round(time.time() - start_time, 1)
            log_msg = f"[{input_dir.name}] 已停止 ({e.step_name})"
            print(f"[info] {log_msg}")
            with PROGRESS_LOCK:
                if STATE["progress"]["run_id"] != run_id:
                    return
                b = STATE["progress"]["batches"][batch_index]
                b["status"] = "stopped"
                b["elapsed_seconds"] = elapsed
            continue
        except PipelineError as e:
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

        result = _compute_batch_result(batch_index, output_dir, media_files)
        elapsed = round(time.time() - start_time, 1)

        with PROGRESS_LOCK:
            if STATE["progress"]["run_id"] != run_id:
                return
            b = STATE["progress"]["batches"][batch_index]
            b["status"] = "done"
            b["elapsed_seconds"] = elapsed
            b.update(result)

    # pipeline 全部跑完了 (或被停止了)，但 cmsw 是各自独立的背景线程，
    # 理论上早就跑完了 —— 这里 join 只是保险，确保收尾之前每一个都真的写回 STATE 了。
    # 注意：目前 Stop 只中止 pipeline，不会去打断已经在跑的 auto_cmsw 线程，
    # 因为它跑的是搜索/过滤而不是本地长时间子程式，通常很快就自己结束了；
    # 如果之后发现 cmsw 也需要能被 Stop 打断，要另外给它接 stop_event。
    for t in cmsw_threads:
        t.join()

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

    # 前端 Stop 按钮在按下 Start All Tasks 之前的状态：true 代表这次 run
    # 完全不跑 classify（florence/YOLO/classifier/organizer 全部跳过），
    # 只跑右边的 auto_cmsw。以前这个欄位有传过来，但下面完全没人读，
    # 所以不管有没有按 Stop，florence 模型永远照样载入 —— 这里补上。
    skip_classify = bool(data.get("stopped", False))

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
            "cmsw": None,           # auto_cmsw 分析结果，独立于 pipeline 完成
            "cmsw_status": "pending",   # pending / running / done / error —— 跟左边 status 各走各的
            "cmsw_start_time": None,
            "cmsw_elapsed_seconds": 0,
            "cmsw_output_path": None,  # auto_cmsw 结果存盘的位置，给 /open_cmsw_folder 用
        }
        for idx, (input_dir, media_files) in enumerate(batches)
    ]

    stop_event = threading.Event()

    with PROGRESS_LOCK:
        STATE["runs"] = []
        STATE["stop_event"] = stop_event
        STATE["progress"] = {
            "run_id": run_id,
            "active": True,
            "finished": False,
            "batches": initial_batches,
            "errors": [],
        }

    thread = threading.Thread(
        target=_run_batches_background,
        args=(run_id, batches, stop_event, skip_classify),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "run_id": run_id, "total_batches": len(batches)})


# ────────────────────────────────────────────────────────────────
# 真正的「停止」：不是前端换个按钮文字就算了，而是把当前 run 的
# stop_event set 起来。main.py 的 run_step() 每 0.3 秒会检查一次这个
# event，抓到之后会把还在跑的子程式 terminate/kill 掉，尚未开始的
# 步骤/batch 也不会再启动。之前完全没有这支 route，前端按 Stop 只是
# 换按钮文字、停止画面更新，terminal 里的 subprocess 完全不受影响，
# 会一直跑到自然结束为止。
# ────────────────────────────────────────────────────────────────

@app.route("/stop_run", methods=["POST"])
def stop_run():
    with PROGRESS_LOCK:
        stop_event = STATE.get("stop_event")
        active = STATE["progress"]["active"]
        run_id = STATE["progress"]["run_id"]

    if stop_event is None or not active:
        return jsonify({"ok": False, "error": "没有正在跑的任务"}), 400

    stop_event.set()
    return jsonify({"ok": True, "run_id": run_id})


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
            start_time = entry.get("start_time")
            # running 中的批次，elapsed 要即时算，不然轮询画面上的秒数不会跳动
            if entry["status"] == "running" and start_time:
                entry["elapsed_seconds"] = round(now - start_time, 1)
            # cmsw 是完全独立的一条线，自己的开始时间跟 pipeline 不一样，
            # 跑的时候一样要即时算它自己的 elapsed
            cmsw_start_time = entry.get("cmsw_start_time")
            if entry.get("cmsw_status") == "running" and cmsw_start_time:
                entry["cmsw_elapsed_seconds"] = round(now - cmsw_start_time, 1)
            entry.pop("start_time", None)       # 内部用的 epoch 时间戳，不用传给前端
            entry.pop("cmsw_start_time", None)
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

def _open_in_file_manager(folder):
    if sys.platform == "win32":
        os.startfile(str(folder))
    elif sys.platform == "darwin":
        os.system(f'open "{folder}"')
    else:
        os.system(f'xdg-open "{folder}"')


@app.route("/open_folder/<int:batch_index>")
@app.route("/open_folder/<int:batch_index>/<path:target>")
def open_folder(batch_index, target="root"):
    if batch_index >= len(STATE["runs"]):
        return jsonify({"ok": False, "error": "batch not found"}), 404

    organized_dir = STATE["runs"][batch_index]["output_dir"] / "organized_photos"
    folder = organized_dir if target == "root" else organized_dir / target

    if not folder.exists():
        return jsonify({"ok": False, "error": "folder not found"}), 404

    _open_in_file_manager(folder)
    return jsonify({"ok": True})

@app.route("/open_cmsw_folder/<int:batch_index>")
def open_cmsw_folder(batch_index):
    if batch_index >= len(STATE["runs"]):
        return jsonify({"ok": False, "error": "batch not found"}), 404

    folder = STATE["runs"][batch_index]["output_dir"]
    if not folder.exists():
        return jsonify({"ok": False, "error": "folder not found"}), 404

    _open_in_file_manager(folder)
    return jsonify({"ok": True})


# ────────────────────────────────────────────────────────────────
# Content Generate (info generate 面板)：把贴进文字框的连结丢给
# auto_report 那套 pipeline (抓网页内容 -> Ollama 中文抽取 -> 翻英文)，
# 同步跑完直接把结果回传给前端显示。存盘位置先不管，只负责生成 + 显示。
# ────────────────────────────────────────────────────────────────

@app.route("/generate_content", methods=["POST"])
def generate_content():
    payload = request.get_json(force=True) or {}
    links_text = payload.get("links_text", "")

    links = extract_links_from_text(links_text)
    if not links:
        return jsonify({"ok": False, "error": "没有找到有效的 http(s) 连结"}), 400

    try:
        data, failed_links = generate_from_links(links)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "data": data, "failed_links": failed_links})


if __name__ == "__main__":
    # threaded=True: /progress 轮询需要在背景 pipeline 执行绪还在跑的时候
    # 也能被同时处理，不然网页会卡住看不到进度。
    app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False, threaded=True)