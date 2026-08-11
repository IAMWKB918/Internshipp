import argparse
import json
import os
import shutil
import sys

# 强制 UTF-8 环境
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_caption_text(entry):
    return " ".join([
        entry.get("caption_short") or "",
        entry.get("caption_detailed") or "",
        entry.get("caption_combined") or "",
    ])

def classify_one(entry, categories, default_category):
    file_name = entry.get("file", "unknown")
    caption_text = get_caption_text(entry).lower()
    object_statistics = entry.get("object_statistics", {}) or {}
    
    # --- 核心诊断逻辑 ---
    florence_count = entry.get("num_people_detected", 0) or 0
    yolo_count = entry.get("yolo_person_count") # 注意：如果JSON里是null，这里就是None
    
    # 最终判定人数
    person_count = max(florence_count, yolo_count) if yolo_count is not None else florence_count
    
    # 在控制台打印诊断信息，帮你一眼看出数据读到没
    yolo_str = yolo_count if yolo_count is not None else "MISSING(null)"
    print(f"  [DEBUG] {file_name} -> Florence:{florence_count}, YOLO:{yolo_str} -> Final:{person_count}")
    # -------------------

    florence_ratio = entry.get("real_person_max_area_ratio", 0.0) or 0.0
    yolo_ratio = entry.get("yolo_max_person_area_ratio")
    area_ratio = max(florence_ratio, yolo_ratio) if yolo_ratio is not None else florence_ratio
    caption_hints = set(entry.get("caption_hints", []) or [])

    for cat in categories:
        name = cat["name"]
        exclude_hints = set(cat.get("exclude_caption_hints", []) or [])
        if exclude_hints & caption_hints:
            continue
        require_hints = set(cat.get("require_caption_hints", []) or [])
        if require_hints and not (require_hints & caption_hints):
            continue

        ocr_kws = cat.get("ocr_keywords", []) or []
        caption_kws = cat.get("caption_keywords", []) or []
        scene_objs = cat.get("scene_objects", []) or []
        content_defined = bool(ocr_kws or caption_kws or scene_objs)

        reasons = []
        content_matched = False
        if content_defined:
            for kw in ocr_kws:
                if kw.lower() in caption_text: # 简化逻辑，只查caption
                    content_matched = True
                    reasons.append(f"kw:{kw}")
                    break
            if not content_matched:
                for obj in scene_objs:
                    if object_statistics.get(obj, 0) > 0:
                        content_matched = True
                        reasons.append(f"obj:{obj}")
                        break
            if not content_matched:
                continue

        min_p = cat.get("min_person_count")
        max_p = cat.get("max_person_count")
        min_ratio = cat.get("min_real_person_area_ratio")
        
        thresholds_ok = True
        if min_p is not None and person_count < min_p: thresholds_ok = False
        if max_p is not None and person_count > max_p: thresholds_ok = False
        if min_ratio is not None and area_ratio < min_ratio: thresholds_ok = False

        if not thresholds_ok:
            continue

        # 匹配成功
        res_reason = "+".join(reasons) if reasons else f"count={person_count}"
        return name, res_reason

    return default_category, "no_rule"

def find_source_file(images_dir, file_stem):
    if not images_dir or not os.path.isdir(images_dir): return None
    for fname in os.listdir(images_dir):
        stem, _ext = os.path.splitext(fname)
        if stem.lower() == file_stem.lower(): # 忽略大小写匹配
            return os.path.join(images_dir, fname)
    return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--aggregated", default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\aggregated_for_llm.json")
    parser.add_argument("--config", default=r"C:\Users\wkb75\Documents\intern cck record\florence\config.json")
    parser.add_argument("--images-dir", default=r"C:\Users\wkb75\Documents\intern cck record\florence\input")
    parser.add_argument("--output-dir", default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\sorted")
    args = parser.parse_args()

    if not os.path.exists(args.aggregated):
        print(f"错误: 找不到JSON文件 {args.aggregated}")
        return

    entries = load_json(args.aggregated)
    cfg = load_json(args.config)
    categories = cfg.get("categories", [])
    default_category = cfg.get("default_category", "Unknown")

    print(f"开始处理 {len(entries)} 张图片...")
    
    manifest = []
    for entry in entries:
        file_stem = entry.get("file", "unknown")
        category, reason = classify_one(entry, categories, default_category)
        
        print(f"  >> Result: {category} ({reason})")

        target_dir = os.path.join(args.output_dir, category)
        os.makedirs(target_dir, exist_ok=True)

        src = find_source_file(args.images_dir, file_stem)
        if src:
            shutil.copy2(src, os.path.join(target_dir, os.path.basename(src)))
            manifest.append({"file": file_stem, "category": category})

    print(f"完成！分类结果存放在: {args.output_dir}")

if __name__ == "__main__":
    main()