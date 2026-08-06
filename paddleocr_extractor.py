#!/usr/bin/env python3
import argparse
import glob
import json
import os
import sys

from tqdm import tqdm
try:
    from paddleocr import PaddleOCR
except ImportError:
    print("错误: 未找到 paddleocr 库。请运行: pip install paddlepaddle paddleocr")
    sys.exit(1)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

_ocr_engine = None

def get_engine():
    global _ocr_engine
    if _ocr_engine is None:
        # 初始化时开启角度分类，ocr() 方法调用时不再传入 cls 参数
        _ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _ocr_engine

def extract_raw_text(image_path: str) -> dict:
    engine = get_engine()
    # 移除了 cls=True，因为它在某些版本中不被支持
    result = engine.ocr(image_path)

    lines = []
    if result and result[0]:
        for entry in result[0]:
            # entry 格式通常为: [bbox, (text, confidence)]
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

    # 排序逻辑
    lines.sort(key=lambda l: (round(l["bbox"][0][1] / 10), l["bbox"][0][0]))
    raw_text = " ".join(l["text"] for l in lines)

    return {
        "file": image_path,
        "raw_text": raw_text,
        "lines": lines,
    }

def main():
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

if __name__ == "__main__":
    main()