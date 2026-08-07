<<<<<<< HEAD
import json
import os
import shutil
import sys
from pathlib import Path

# --- Force terminal to use UTF-8 for Chinese characters ---
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

# ================= CONFIGURATION =================
MANIFEST_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\sorted\classify_manifest.json"
IMAGES_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\input"
OUTPUT_ROOT = r"C:\Users\wkb75\Documents\intern cck record\florence\output\organized_photos"
# =================================================

def find_image_file(stem, search_dir):
    search_path = Path(search_dir)
    valid_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.JPG', '.JPEG', '.PNG'}
    
    if not search_path.exists():
        return None

    for file in search_path.rglob('*'):
        if file.is_file() and file.stem == stem and file.suffix in valid_extensions:
            return file
    return None

def main():
    print("="*50)
    print("IMAGE ORGANIZER & RENAMER STARTING")
    print("="*50)
    
    if not os.path.exists(MANIFEST_PATH):
        print(f"ERROR: Manifest file not found!")
        return

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    success_count = 0
    fail_count = 0

    for entry in manifest:
        file_stem = entry.get("file")
        year = str(entry.get("year", "Unknown_Year"))
        category = entry.get("category", "Uncategorized")

        # Step 1: Find the original file
        src_image = find_image_file(file_stem, IMAGES_DIR)
        
        if not src_image:
            print(f"[NOT FOUND] {file_stem}")
            fail_count += 1
            continue

        # Step 2: Create target directory
        target_dir = Path(OUTPUT_ROOT) / year / category
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # Step 3: CONSTRUCT NEW FILENAME (The Rename Part)
        # Format: Year_Category_OriginalName.extension
        new_filename = f"{year}_{category}_{src_image.name}"
        dest_path = target_dir / new_filename

        # Step 4: Copy and Overwrite (No more doubles!)
        try:
            # shutil.copy2 will overwrite if the file exists
            shutil.copy2(src_image, dest_path)
            
            msg = f"[OK] Renamed: {new_filename}"
            print(msg.encode('utf-8', errors='replace').decode('utf-8'))
            success_count += 1
        except Exception as e:
            print(f"[ERROR] Failed to process {src_image.name}: {str(e)}")
            fail_count += 1

    print("="*50)
    print(f"Final Result: {success_count} images organized and renamed.")
    print(f"Check your folder: {OUTPUT_ROOT}")
    print("="*50)
=======
import sys
import json
import shutil
from pathlib import Path
from datetime import datetime

from PIL import Image
from PIL.ExifTags import TAGS

# Windows 终端默认编码常常不是 UTF-8（比如 cp1252），
# 直接打印中文会报 UnicodeEncodeError，这里强制改成 UTF-8。
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass  # Python < 3.7，没有 reconfigure，忽略

# ============================================================
# PATHS  —— 按你实际的文件夹结构调整
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

# 存放所有 classification.json 的文件夹（Step 5 的输出）
ANALYSIS_DIR = BASE_DIR / "analysis"

# 原图所在的文件夹 —— 如果原图不在这里，会在其下递归搜索同名文件
IMAGES_DIR = BASE_DIR / "input"

# 整理后的输出根目录，结构为 output/日期/分类名/文件名
OUTPUT_DIR = BASE_DIR / "organized"

# 整理日志
LOG_FILE = ANALYSIS_DIR / "organize_log.json"


# ============================================================
# LOAD JSON
# ============================================================

def load_json(path):
    """Load a JSON file safely, return None on failure."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARNING] Failed to load {path}: {e}")
        return None


# ============================================================
# FIND CLASSIFICATION RESULT FILES
# ============================================================

def find_classification_files(analysis_dir):
    """
    扫描 analysis_dir 下所有 json，只保留同时具备
    'category' 和 'filename' 字段的（即 classifier.py 的输出）。
    Florence 原始分析结果（没有 'category'）会被自动跳过。
    """
    candidates = []

    for path in Path(analysis_dir).glob("*.json"):
        data = load_json(path)
        if data is None:
            continue

        if "category" in data and "filename" in data:
            candidates.append((path, data))

    return candidates


# ============================================================
# LOCATE ORIGINAL IMAGE
# ============================================================

def resolve_image_path(filename, images_dir):
    """
    先直接在 images_dir 下找同名文件；找不到就递归搜索整个 images_dir。
    返回 Path 或 None。
    """
    images_dir = Path(images_dir)

    direct = images_dir / filename
    if direct.exists():
        return direct

    for match in images_dir.rglob(filename):
        return match

    return None


# ============================================================
# DATE RESOLUTION
# （目前 organize_one() 没有调用这些函数——先把分类跑对，
#  以后要按日期分层时，在 organize_one() 里把 dest_dir
#  改成 Path(output_dir) / date_folder / category 即可复用）
# ============================================================

def get_exif_date(image_path):
    """
    尝试读取 EXIF 的拍摄日期 (DateTimeOriginal)。
    读取失败或没有该字段则返回 None。
    """
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            if not exif:
                return None

            for tag_id, value in exif.items():
                tag_name = TAGS.get(tag_id, tag_id)
                if tag_name == "DateTimeOriginal" or tag_name == "DateTime":
                    # EXIF 日期格式: "YYYY:MM:DD HH:MM:SS"
                    return datetime.strptime(
                        value, "%Y:%m:%d %H:%M:%S"
                    ).date()

    except Exception as e:
        print(f"[WARNING] EXIF read failed for {image_path}: {e}")

    return None


def get_file_mtime_date(image_path):
    """退回方案：使用文件系统的修改日期。"""
    timestamp = Path(image_path).stat().st_mtime
    return datetime.fromtimestamp(timestamp).date()


def resolve_date(image_path):
    """优先 EXIF 拍摄日期，没有则用文件修改日期。"""
    exif_date = get_exif_date(image_path)
    if exif_date:
        return exif_date, "exif"

    return get_file_mtime_date(image_path), "mtime"


# ============================================================
# SAFE COPY (避免覆盖同名文件)
# ============================================================

def unique_destination(dest_path):
    """如果目标文件已存在，自动加 (1)、(2)... 后缀。"""
    if not dest_path.exists():
        return dest_path

    stem = dest_path.stem
    suffix = dest_path.suffix
    parent = dest_path.parent

    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ============================================================
# ORGANIZE ONE IMAGE
# ============================================================

def organize_one(json_path, data, images_dir, output_dir):
    """
    处理单条分类结果：定位原图 -> 复制到 output_dir/分类名/ 下。
    （日期分层先不做，专注把分类先跑对）
    返回处理日志字典。
    """
    filename = data.get("filename", "unknown")
    category = data.get("category", "Other")

    log = {
        "json": str(json_path),
        "filename": filename,
        "category": category,
        "status": None,
        "destination": None,
        "error": None
    }

    image_path = resolve_image_path(filename, images_dir)

    if image_path is None:
        log["status"] = "skipped_missing_image"
        log["error"] = f"Image not found under {images_dir}"
        print(f"[SKIP] {filename}: 找不到原图")
        return log

    try:
        dest_dir = Path(output_dir) / category
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_path = unique_destination(dest_dir / image_path.name)

        shutil.copy2(image_path, dest_path)

        log["status"] = "copied"
        log["destination"] = str(dest_path)

        print(f"[OK] {filename} -> {dest_path}")

    except Exception as e:
        log["status"] = "error"
        log["error"] = str(e)
        print(f"[ERROR] {filename}: {e}")

    return log


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("IMAGE ORGANIZER")
    print("=" * 60)

    if not IMAGES_DIR.exists():
        print(f"[WARNING] IMAGES_DIR 不存在: {IMAGES_DIR}")
        print("请检查脚本顶部的 IMAGES_DIR 是否指向正确的原图文件夹。")

    classification_files = find_classification_files(ANALYSIS_DIR)
    print(f"找到 {len(classification_files)} 份分类结果。\n")

    logs = []

    for json_path, data in classification_files:
        log = organize_one(json_path, data, IMAGES_DIR, OUTPUT_DIR)
        logs.append(log)

    # --- Summary ---
    copied = sum(1 for l in logs if l["status"] == "copied")
    skipped = sum(1 for l in logs if l["status"] == "skipped_missing_image")
    errors = sum(1 for l in logs if l["status"] == "error")

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"成功复制: {copied}")
    print(f"找不到原图跳过: {skipped}")
    print(f"出错: {errors}")

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=4, ensure_ascii=False)

    print(f"\n详细日志已保存到: {LOG_FILE}")

>>>>>>> a1e08f1a21dc5f10447ccfd07aafb99dd5fbb277

if __name__ == "__main__":
    main()