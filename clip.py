import fitz
import easyocr
import os
import json
import shutil
import sys
import subprocess
import numpy as np
import io
import gc
import torch 
from flask import Flask, render_template, request, jsonify, send_from_directory
from pathlib import Path
from PIL import Image
from sentence_transformers import SentenceTransformer, util

# 初始化 EasyOCR (你原有的代码已经有了，保持不变)
# gpu=True 如果你的显卡显存够，建议开启；如果不稳可以改 False
reader = easyocr.Reader(['ch_sim', 'en'], gpu=torch.cuda.is_available()) 

# 初始化 Flask 和 CLIP
app = Flask(__name__, static_folder='static')
TEMP_DIR = Path("pending_pool")
PREVIEW_DIR = Path("static/previews")
for d in [TEMP_DIR, PREVIEW_DIR]: d.mkdir(parents=True, exist_ok=True)

print("Loading CLIP Model...")
device = "cuda" if torch.cuda.is_available() else "cpu"
model = SentenceTransformer('clip-ViT-B-32', device=device)

# --- 修改后的 OCR 函数：使用 EasyOCR ---
def extract_text_easyocr(image):
    try:
        w, h = image.size
        image = image.resize((w*2, h*2), Image.LANCZOS)
        
        img_np = np.array(image.convert('L')) 
        
        results = reader.readtext(img_np, detail=0)
        full_text = " ".join(results).lower()
        print(f"OCR Result: {full_text}") 
        return full_text
    except Exception as e:
        print(f"EasyOCR Error: {e}")
        return ""

# --- 核心：修复后的帧提取函数 (完全保持不变) ---
def extract_video_frames_ffmpeg(video_path, num_frames=6):
    try:
        cmd_duration = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)]
        duration = float(subprocess.check_output(cmd_duration).decode('utf-8').strip())
        
        frames = []
        times = np.linspace(0.5, max(0.5, duration - 0.5), num_frames)
        
        for t in times:
            cmd = ['ffmpeg', '-y', '-ss', str(t), '-i', str(video_path), '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-']
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, _ = process.communicate()
            if stdout:
                frames.append(Image.open(io.BytesIO(stdout)).convert('RGB'))
        return frames
    except Exception as e:
        print(f"FFmpeg Error: {e}")
        return []

@app.route('/')
def index(): return render_template('clip.html')

@app.route('/open_folder/<folder_name>')
def open_folder(folder_name):
    with open('clip.json', 'r', encoding='utf-8') as f: config = json.load(f)
    base_path = Path(config['output_path'])
    target = base_path if folder_name == "root" else base_path / folder_name
    if target.exists(): os.startfile(target)
    return jsonify({"status": "opened"})

@app.route('/select_folder', methods=['POST'])
def select_folder():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    path = filedialog.askdirectory(); root.destroy()
    return jsonify({"path": path if path else ""})

@app.route('/upload_temp', methods=['POST'])
def upload_temp():
    files = request.files.getlist('files[]')
    saved_paths = []
    for f in files:
        p = TEMP_DIR / f.filename
        f.save(p)
        saved_paths.append(str(p))
    return jsonify({"paths": saved_paths})

@app.route('/run_tasks', methods=['POST'])
def run_tasks():
    tasks = request.json.get('tasks', [])
    with open('clip.json', 'r', encoding='utf-8') as f: config = json.load(f)
    output_dir = Path(config['output_path'])
    output_dir.mkdir(exist_ok=True)

    flat_prompts = [p for cat in config['categories'] for p in cat['prompts']]
    text_emb = model.encode(flat_prompts, convert_to_tensor=True)

    logs, folder_counts = [], {}
    final_list = []
    for t in tasks:
        p = Path(t['path'])
        if t['type'] == 'folder':
            for ext in ["*.jpg", "*.png", "*.jpeg", "*.mp4", "*.avi", "*.mov", "*.mkv"]:
                for f in p.glob(ext): final_list.append({"path": f, "is_temp": False})
        else: final_list.append({"path": p, "is_temp": True})

    # 清理旧预览图
    for f in PREVIEW_DIR.glob("thumb_*.jpg"):
        try: os.remove(f)
        except: pass

    for item in final_list:
        f_path = item['path']
        try:
            is_video = f_path.suffix.lower() in [".mp4", ".avi", ".mov", ".mkv", ".flv"]
            temp_images = extract_video_frames_ffmpeg(f_path, 6) if is_video else [Image.open(f_path).convert('RGB')]
            
            if not temp_images: continue
            
            # 1. 跑原始 CLIP 逻辑
            img_embs = model.encode(temp_images, convert_to_tensor=True)
            cos_sims = util.cos_sim(img_embs, text_emb)
            
            # 找到视觉相似度最高的帧
            best_frame_idx = torch.argmax(torch.max(cos_sims, dim=1)[0]).item()
            best_scores_per_prompt, _ = torch.max(cos_sims, dim=0)
            max_val, best_prompt_idx = torch.max(best_scores_per_prompt).item(), torch.argmax(best_scores_per_prompt).item()

            target = "Missing"
            
            # --- 2. 使用 EasyOCR 进行文字关键词检索 ---
            best_image = temp_images[best_frame_idx]
            detected_text = extract_text_easyocr(best_image)
            found_by_ocr = False
            
            for cat in config['categories']:
                keywords = cat.get('keywords', [])
                for kw in keywords:
                    if kw.lower() in detected_text:
                        target = cat['name']
                        max_val = 1.0  # 文字匹配成功，置信度满分
                        found_by_ocr = True
                        break
                if found_by_ocr: break

            # --- 3. 如果文字没匹配到，才走 CLIP 视觉 ---
            if not found_by_ocr and max_val >= config['threshold']:
                curr = 0
                for cat in config['categories']:
                    if curr <= best_prompt_idx < curr + len(cat['prompts']):
                        target = cat['name']; break
                    curr += len(cat['prompts'])
            
            # 保存预览图 (逻辑保持不变)
            import hashlib
            safe_name = hashlib.md5(f_path.name.encode()).hexdigest()
            preview_filename = f"thumb_{safe_name}.jpg"
            best_image.save(PREVIEW_DIR / preview_filename, "JPEG", quality=70)
            
            # 创建文件夹并移动/复制
            (output_dir / target).mkdir(parents=True, exist_ok=True)
            folder_counts[target] = folder_counts.get(target, 0) + 1
            new_name = f"{target}_({folder_counts[target]}){f_path.suffix}"
            
            if item['is_temp']: shutil.move(str(f_path), output_dir / target / new_name)
            else: shutil.copy(f_path, output_dir / target / new_name)

            logs.append({
                "file": f_path.name, 
                "result": target, 
                "score": round(max_val, 4),
                "thumb_url": f"/static/previews/{preview_filename}"
            })
        except Exception as e: print(f"Error processing {f_path}: {e}")
        finally:
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()

    return jsonify({"logs": logs, "summary": folder_counts, "output_path": str(output_dir)})

if __name__ == '__main__':
    app.run(debug=False, port=5000)