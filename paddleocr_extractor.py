#!/usr/bin/env python3
import argparse
import glob
import json
import os
import sys

<<<<<<< HEAD
# --- Force terminal to use UTF-8 to prevent encoding errors on Windows ---
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

from tqdm import tqdm

try:
    from paddleocr import PaddleOCR
except ImportError:
    print("Error: paddleocr library not found. Please run: pip install paddlepaddle paddleocr")
    sys.exit(1)

# ================= PATH CONFIGURATION =================
# Locked input directory containing images
DEFAULT_INPUT_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\input"

# Locked output JSON file path
DEFAULT_OUTPUT_FILE = r"C:\Users\wkb75\Documents\intern cck record\florence\output\paddle_results.json"
# ======================================================

=======
from tqdm import tqdm
try:
    from paddleocr import PaddleOCR
except ImportError:
    print("错误: 未找到 paddleocr 库。请运行: pip install paddlepaddle paddleocr")
    sys.exit(1)

>>>>>>> a1e08f1a21dc5f10447ccfd07aafb99dd5fbb277
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

_ocr_engine = None

def get_engine():
<<<<<<< HEAD
    """Initializes the OCR engine lazily."""
    global _ocr_engine
    if _ocr_engine is None:
        # use_angle_cls=True enables direction classification
=======
    global _ocr_engine
    if _ocr_engine is None:
        # 初始化时开启角度分类，ocr() 方法调用时不再传入 cls 参数
>>>>>>> a1e08f1a21dc5f10447ccfd07aafb99dd5fbb277
        _ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _ocr_engine

def extract_raw_text(image_path: str) -> dict:
<<<<<<< HEAD
    """Extracts text from a single image and returns a structured dictionary."""
    engine = get_engine()
=======
    engine = get_engine()
    # 移除了 cls=True，因为它在某些版本中不被支持
>>>>>>> a1e08f1a21dc5f10447ccfd07aafb99dd5fbb277
    result = engine.ocr(image_path)

    lines = []
    if result and result[0]:
        for entry in result[0]:
<<<<<<< HEAD
            # entry format: [bbox, (text, confidence)]
=======
            # entry 格式通常为: [bbox, (text, confidence)]
>>>>>>> a1e08f1a21dc5f10447ccfd07aafb99dd5fbb277
            if len(entry) == 2:
                bbox, (text, confidence) = entry
                text = text.strip()
                if not text:
                    continue
                lines.append({
                    "text": text,
                    "confidence": round(float(confidence), 4),
                    "bbox": bbox,
                })

<<<<<<< HEAD
    # Sort lines: Primary sort by Y-axis (top-to-bottom), Secondary by X-axis (left-to-right)
=======
    # 排序逻辑
>>>>>>> a1e08f1a21dc5f10447ccfd07aafb99dd5fbb277
    lines.sort(key=lambda l: (round(l["bbox"][0][1] / 10), l["bbox"][0][0]))
    raw_text = " ".join(l["text"] for l in lines)

    return {
        "file": image_path,
        "raw_text": raw_text,
        "lines": lines,
    }

def main():
<<<<<<< HEAD
    parser = argparse.ArgumentParser(description="PaddleOCR Text Extractor (Locked Paths)")
    parser.add_argument("--dir", default=DEFAULT_INPUT_DIR, help="Directory containing source images")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_FILE, help="Path to the output JSON file")
    args = parser.parse_args()

    # Create output directory if it does not exist
    output_dir = os.path.dirname(args.out)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    # Validate input directory
    if not os.path.exists(args.dir):
        print(f"ERROR: Input directory not found -> {args.dir}")
        return

    print("=" * 50)
    print("PADDLE OCR EXTRACTOR STARTING")
    print(f"Input Directory:  {args.dir}")
    print(f"Output File:       {args.out}")
    print("=" * 50)

    # Gather all supported image files
    files = sorted(f for f in glob.glob(os.path.join(args.dir, "*")) if f.lower().endswith(IMAGE_EXTS))
    
    if not files:
        print("No image files found in the input directory.")
        return

    results = []
    for f in tqdm(files, desc="OCR Processing"):
        try:
            results.append(extract_raw_text(f))
        except Exception as e:
            results.append({
                "file": f, 
                "raw_text": "", 
                "lines": [], 
                "error": str(e)
            })
    
    # Save results to JSON
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        
        print("-" * 50)
        print("Process Complete!")
        print(f"Total Images: {len(results)}")
        print(f"Manifest saved to: {args.out}")
    except Exception as e:
        print(f"ERROR: Failed to write JSON file: {e}")
=======
    parser = argparse.ArgumentParser(description="PaddleOCR 中文抓取")
    parser.add_argument("--file", help="单张图片路径")
    parser.add_argument("--dir", help="图片文件夹路径")
    parser.add_argument("--out", default="paddle_results.json", help="输出JSON文件路径 (必须是文件)")
    args = parser.parse_args()

    if not args.file and not args.dir:
        parser.error("必须指定 --file 或 --dir")

    # 确保输出路径不是一个现有的文件夹
    if os.path.isdir(args.out):
        parser.error(f"输出路径 '{args.out}' 是一个文件夹，请输入一个文件路径 (例如 output.json)")

    if args.file:
        result = extract_raw_text(args.file)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([result], f, ensure_ascii=False, indent=2)
        print(f"结果已保存至: {args.out}")

    if args.dir:
        files = sorted(f for f in glob.glob(os.path.join(args.dir, "*")) if f.lower().endswith(IMAGE_EXTS))
        results = []
        for f in tqdm(files, desc="处理中"):
            try:
                results.append(extract_raw_text(f))
            except Exception as e:
                results.append({"file": f, "raw_text": "", "lines": [], "error": str(e)})
        
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"共处理 {len(results)} 张，结果存到: {args.out}")
>>>>>>> a1e08f1a21dc5f10447ccfd07aafb99dd5fbb277

if __name__ == "__main__":
    main()