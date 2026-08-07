#!/usr/bin/env python3
import argparse
import glob
import json
import os
import sys

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

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

_ocr_engine = None

def get_engine():
    """Initializes the OCR engine lazily."""
    global _ocr_engine
    if _ocr_engine is None:
        # use_angle_cls=True enables direction classification
        _ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _ocr_engine

def extract_raw_text(image_path: str) -> dict:
    """Extracts text from a single image and returns a structured dictionary."""
    engine = get_engine()
    result = engine.ocr(image_path)

    lines = []
    if result and result[0]:
        for entry in result[0]:
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

    lines.sort(key=lambda l: (round(l["bbox"][0][1] / 10), l["bbox"][0][0]))
    raw_text = " ".join(l["text"] for l in lines)

    return {
        "file": image_path,
        "raw_text": raw_text,
        "lines": lines,
    }

def main():
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

if __name__ == "__main__":
    main()