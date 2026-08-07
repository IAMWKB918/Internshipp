import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# 处理 Windows 终端中文显示问题
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

# ================= CONFIGURATION (默认值) =================
MANIFEST_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\sorted\classify_manifest.json"
IMAGES_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\input"
OUTPUT_ROOT = r"C:\Users\wkb75\Documents\intern cck record\florence\output\organized_photos"
# =========================================================

def parse_args():
    parser = argparse.ArgumentParser(description="按 classifier 的分类结果复制+改名归档图片")
    parser.add_argument("--manifest", default=MANIFEST_PATH, help="classify_manifest.json 路径")
    parser.add_argument("--images-dir", default=IMAGES_DIR, help="原始图片所在文件夹")
    parser.add_argument("--output-root", default=OUTPUT_ROOT, help="归档输出的根目录")
    return parser.parse_args()

def find_image_file(stem, search_dir):
    """
    在指定目录及其子目录中查找匹配文件名的图片，不区分大小写
    """
    search_path = Path(search_dir)
    # 统一转为小写以方便匹配
    valid_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
    
    if not search_path.exists():
        print(f"[ERROR] 搜索路径不存在: {search_dir}")
        return None

    # 如果传入的 stem 带有后缀（如 x.jpg），先去掉它只取名字部分
    clean_stem = Path(stem).stem.lower()

    for file in search_path.rglob('*'):
        if file.is_file():
            # 比较文件名（转小写）且 后缀在有效范围内
            if file.stem.lower() == clean_stem and file.suffix.lower() in valid_extensions:
                return file
    return None

def main():
    args = parse_args()

    print("="*50)
    print("IMAGE ORGANIZER & RENAMER STARTING")
    print(f"Manifest: {args.manifest}")
    print(f"Searching in: {args.images_dir}")
    print("="*50)
    
    if not os.path.exists(args.manifest):
        print(f"ERROR: Manifest file not found at {args.manifest}")
        return

    try:
        with open(args.manifest, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to read JSON: {e}")
        return

    success_count = 0
    fail_count = 0

    for entry in manifest:
        # 获取文件名、年份、类别
        raw_file_name = entry.get("file")
        if not raw_file_name:
            continue

        year = str(entry.get("year", "Unknown_Year"))
        category = entry.get("category", "Unclassified")

        # Step 1: 查找原始文件 (不区分大小写)
        src_image = find_image_file(raw_file_name, args.images_dir)
        
        if not src_image:
            print(f"[NOT FOUND] 找不到图片: {raw_file_name}")
            fail_count += 1
            continue

        # Step 2: 创建目标目录 (organized_photos/年份/类别)
        target_dir = Path(args.output_root) / year / category
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # Step 3: 构建新文件名
        # 格式: 年份_类别_原始文件名.后缀
        new_filename = f"{year}_{category}_{src_image.name}"
        dest_path = target_dir / new_filename

        # Step 4: 复制并覆盖
        try:
            shutil.copy2(src_image, dest_path)
            print(f"[OK] 已归档: {new_filename}")
            success_count += 1
        except Exception as e:
            print(f"[ERROR] 复制失败 {src_image.name}: {str(e)}")
            fail_count += 1

    print("="*50)
    print(f"整理完成！成功: {success_count}, 失败: {fail_count}")
    print(f"目标位置: {args.output_root}")
    print("="*50)

if __name__ == "__main__":
    main()