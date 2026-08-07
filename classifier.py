<<<<<<< HEAD
import argparse
import json
import os
import shutil
import sys  # 必须导入 sys 模块

# ================= 修复中文报错的关键部分 =================
# 强制 Windows 终端使用 UTF-8 编码，防止打印中文时崩溃
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # 兼容旧版本 Python
        pass
# =========================================================

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_ocr_text(entry):
    """Concatenate florence/paddle raw text and keywords into one string for substring matching."""
    ocr = entry.get("ocr", {}) or {}
    parts = [
        ocr.get("florence_raw") or "",
        ocr.get("paddle_raw") or "",
    ]
    parts += ocr.get("keywords") or []
    parts += ocr.get("paddle_high_confidence_lines") or []
    return " ".join(parts)


def get_caption_text(entry):
    vd = entry.get("visual_description", {}) or {}
    return " ".join([
        vd.get("short") or "",
        vd.get("detailed") or "",
        vd.get("most_detailed") or "",
    ])


def get_person_count(entry):
    scene = entry.get("scene_stats", {}) or {}
    return (scene.get("person", 0) or 0) + (scene.get("human face", 0) or 0)


def classify_one(entry, categories, default_category):
    """Walk the categories list in order; the first matching rule wins. No scoring."""
    ocr_text = get_ocr_text(entry).lower()
    caption_text = get_caption_text(entry).lower()
    scene = entry.get("scene_stats", {}) or {}
    person_count = get_person_count(entry)

    for cat in categories:
        name = cat["name"]

        for kw in cat.get("ocr_keywords", []):
            if kw.lower() in ocr_text:
                return name, f"ocr_keyword:{kw}"

        for kw in cat.get("caption_keywords", []):
            if kw.lower() in caption_text:
                return name, f"caption_keyword:{kw}"

        for obj in cat.get("scene_objects", []):
            if scene.get(obj, 0) > 0:
                return name, f"scene_object:{obj}"

        min_p = cat.get("min_person_count")
        if min_p is not None and person_count >= min_p:
            return name, f"person_count>={min_p}"

    return default_category, "no_rule_matched"


def get_year(entry):
    """
    重新定义年份获取优先级:
    1. EXIF (exif_datetime_original)
    2. OCR (possible_years)
    3. FileSystem (inferred_year)
    """
    time_info = entry.get("time_info", {})
    ocr_info = entry.get("ocr", {}) or {}

    exif_dt = time_info.get("exif_datetime_original")
    if exif_dt and isinstance(exif_dt, str) and len(exif_dt) >= 4:
        return exif_dt[:4]

    possible_years = ocr_info.get("possible_years", [])
    if possible_years and len(possible_years) > 0:
        return str(possible_years[0])

    return time_info.get("inferred_year") or "unknown_year"
    
def find_source_file(images_dir, file_stem):
    if not images_dir or not os.path.isdir(images_dir):
        return None
    exact = os.path.join(images_dir, file_stem)
    if os.path.isfile(exact):
        return exact
    for fname in os.listdir(images_dir):
        stem, _ext = os.path.splitext(fname)
        if stem == file_stem:
            return os.path.join(images_dir, fname)
    return None


def main():
    parser = argparse.ArgumentParser(description="Sort photos into year/category folders")
    
    # 这里保持你要求的锁定路径
    parser.add_argument("--aggregated", 
                        default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\aggregated_for_llm.json")
    
    parser.add_argument("--config", 
                        default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\config.json")
    
    parser.add_argument("--images-dir", 
                        default=r"C:\Users\wkb75\Documents\intern cck record\florence\input")
    
    parser.add_argument("--output-dir", 
                        default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\sorted")
    
    parser.add_argument("--mode", choices=["copy", "symlink"], default="copy")
    parser.add_argument("--dry-run", action="store_true")
    
    args = parser.parse_args()

    entries = load_json(args.aggregated)
    cfg = load_json(args.config)
    categories = cfg.get("categories", [])
    default_category = cfg.get("default_category", "Unknown")
    path_template = cfg.get("output", {}).get("path_template", "{year}/{category}")

    manifest = []
    for entry in entries:
        file_stem = entry.get("file", "unknown")
        year = get_year(entry)
        category, reason = classify_one(entry, categories, default_category)

        record = {
            "file": file_stem,
            "year": year,
            "category": category,
            "match_reason": reason,
        }
        manifest.append(record)

        # 这里增加了编码安全处理，防止打印中文崩溃
        try:
            print(f"{file_stem} -> {year}/{category}  (reason: {reason})")
        except UnicodeEncodeError:
            print(f"{file_stem} -> {year}/[Chinese Category Name]  (reason: {reason})")

        if args.dry_run:
            continue

        relative_dir = path_template.format(year=year, category=category)
        target_dir = os.path.join(args.output_dir, relative_dir)
        os.makedirs(target_dir, exist_ok=True)

        src = find_source_file(args.images_dir, file_stem)
        if not src:
            record["status"] = "source image not found, skipped"
            continue

        dst = os.path.join(target_dir, os.path.basename(src))
        if args.mode == "copy":
            shutil.copy2(src, dst)
        else:
            if os.path.exists(dst):
                os.remove(dst)
            os.symlink(os.path.abspath(src), dst)
        record["status"] = f"{'copied' if args.mode == 'copy' else 'symlinked'} to {dst}"

    manifest_path = os.path.join(args.output_dir if not args.dry_run else ".", "classify_manifest.json")
    os.makedirs(os.path.dirname(manifest_path) or ".", exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("-" * 50)
    print(f"Processed {len(entries)} images. Manifest saved to: {manifest_path}")
=======
#!/usr/bin/env python3
"""
merge_classifier.py
--------------------
把三路信号统一汇总，按照你的 flowchart 决定 final_primary_year：

  1) EXIF DateTimeOriginal (confidence="high")   -> 直接用，最高优先级
  2) EXIF 缺失 -> 看 OCR：
       2a) Florence OCR 抓到的年份 (英文/数字为主)
       2b) PaddleOCR 抓到的年份 (中文背景板，可选，还没跑就传 None)
       两者都有时，取"更常出现/更近的"那个，可自行调整规则
  3) 文件系统时间 -> 永远不参与这里的决策，只在 exif_analysis 里当参考

florence.py / exif_extractor.py 完全不用改，各自继续产各自的 json。
这个脚本是唯一"做最终判断"的地方，以后加 PaddleOCR 也只改这里。

用法:
  python merge_classifier.py \
      --florence-dir input/florence_results \
      --exif-json exif_results.json \
      --paddle-json paddle_results.json \        # 可选，还没有就不传
      --out-dir merged_results
"""

import argparse
import glob
import json
import os


def load_index_by_basename(json_path):
    """exif_results.json / paddle_results.json 都是 list，且带 file 全路径，
    用 basename 建索引方便跟 florence 的单张结果对上。"""
    if not json_path or not os.path.exists(json_path):
        return {}
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    idx = {}
    for item in data:
        basename = os.path.basename(item.get("file", ""))
        if basename:
            idx[basename] = item
    return idx


def decide_final_year(florence_result: dict, exif_item: dict, paddle_item: dict):
    """
    核心优先级判断，唯一真相来源 (single source of truth)。
    返回 (year, source, confidence)
    """
    # 1) EXIF 高置信度 -> 直接赢
    if exif_item and exif_item.get("confidence") == "high" and exif_item.get("year"):
        return exif_item["year"], "exif", "high"

    # 2) EXIF 缺失 -> 看 OCR (florence 英文/数字 + paddle 中文)
    ocr_info = florence_result.get("post_processing", {}).get("ocr_information", {})
    florence_year = ocr_info.get("ocr_primary_year")

    paddle_year = None
    if paddle_item:
        # 约定 paddle_results.json 每条也带一个 "cn_primary_year" 字段，
        # 具体怎么从 paddleocr 原始输出抽年份，等你那边跑起来后我们再对齐格式。
        paddle_year = paddle_item.get("cn_primary_year")

    if florence_year and paddle_year:
        # 两个 OCR 都抓到年份但不一致时，两个都留底，优先取 paddle
        # (中文背景板通常比英文横幅更靠近拍摄现场，比如活动海报)，
        # 但这个规则你可以按实际情况调整。
        chosen = paddle_year if paddle_year != florence_year else florence_year
        return chosen, "ocr_paddle+florence_conflict" if paddle_year != florence_year else "ocr_agreed", "medium"

    if paddle_year:
        return paddle_year, "ocr_paddle", "medium"

    if florence_year:
        return florence_year, "ocr_florence", "medium"

    # 3) 什么都没有 -> 绝不 fallback 到文件系统时间，标记未解决
    return None, "unresolved", "low"


def merge_one(florence_json_path: str, exif_idx: dict, paddle_idx: dict, out_dir: str):
    with open(florence_json_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    file_path = result.get("image_information", {}).get("file_path", "")
    basename = os.path.basename(file_path) if file_path else os.path.basename(florence_json_path)

    exif_item = exif_idx.get(basename)
    paddle_item = paddle_idx.get(basename)

    year, source, confidence = decide_final_year(result, exif_item, paddle_item)

    pp = result.setdefault("post_processing", {})
    pp["exif_analysis"] = exif_item  # None 就代表这张图完全没跑过 exif_extractor
    pp["paddleocr_analysis"] = paddle_item  # None 就代表还没跑 paddleocr
    pp["final_primary_year"] = year
    pp["final_year_source"] = source
    pp["final_year_confidence"] = confidence
    # 保留旧的 physical_metadata (florence.py 自带那个) 当 legacy 参考，
    # 但它不再参与 final_primary_year 的判断
    if "physical_metadata" in pp:
        pp["physical_metadata"]["note"] = "legacy field, no longer authoritative — see exif_analysis"

    out_path = os.path.join(out_dir, os.path.basename(florence_json_path))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return out_path, year, source, confidence


def main():
    parser = argparse.ArgumentParser(description="EXIF / OCR 多路信号年份判断汇总")
    parser.add_argument("--florence-dir", required=True, help="florence_results 文件夹（每张图一个 json）")
    parser.add_argument("--exif-json", required=True, help="exif_extractor.py 批次输出的 json")
    parser.add_argument("--paddle-json", default=None, help="paddleocr 批次输出的 json（可选，暂时可不传）")
    parser.add_argument("--out-dir", default="merged_results")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    exif_idx = load_index_by_basename(args.exif_json)
    paddle_idx = load_index_by_basename(args.paddle_json)

    florence_files = sorted(glob.glob(os.path.join(args.florence_dir, "*.json")))
    if not florence_files:
        print(f"在 {args.florence_dir} 找不到 florence 的 json 结果")
        return

    stats = {"high": 0, "medium": 0, "low": 0}
    for fp in florence_files:
        out_path, year, source, confidence = merge_one(fp, exif_idx, paddle_idx, args.out_dir)
        stats[confidence] += 1
        print(f"{os.path.basename(fp)} -> year={year}, source={source}, confidence={confidence}")

    print("-" * 50)
    print(f"共处理 {len(florence_files)} 张: high={stats['high']}, medium={stats['medium']}, low={stats['low']}")
    print(f"结果已存到: {args.out_dir}")
>>>>>>> a1e08f1a21dc5f10447ccfd07aafb99dd5fbb277


if __name__ == "__main__":
    main()