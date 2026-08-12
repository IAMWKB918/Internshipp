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
    person_count = max(florence_count, yolo_count) if yolo_count is not None else florence_count

    # 最终判定人数
    limb_kws = ['arm', 'hand', 'finger', 'leg', 'foot', 'thumb', 'wrist', 'elbow', 'nail', 'portion of']
    # 完整性特徵詞 (身份/頭部)
    identity_kws = ['face', 'head', 'portrait', 'man', 'woman', 'lady', 'gentleman', 'boy', 'girl', 'standing', 'sitting', 'posing', 'walking']
    
    # 判定是否為「純肢體零件」
    has_limb = any(kw in caption_text for kw in limb_kws)
    has_identity = any(kw in caption_text for kw in identity_kws) or (object_statistics.get("human face", 0) > 0)
    
    # 如果描述中只有肢體詞，卻完全沒提到身份詞或臉，則判定為「零件」，直接歸類到 Default
    if has_limb and not has_identity:
        print(f"  [DEBUG] {file_name} -> 判定為肢體零件 (Limb detected, no identity) -> 跳過 Portrait")
        return default_category, "body_part_detected"
    # -------------------

    florence_ratio = entry.get("real_person_max_area_ratio", 0.0) or 0.0
    yolo_ratio = entry.get("yolo_max_person_area_ratio")
    area_ratio = max(florence_ratio, yolo_ratio) if yolo_ratio is not None else florence_ratio
    caption_hints = set(entry.get("caption_hints", []) or [])

    for cat in categories:
        name = cat["name"]
        
        exclude_kws = cat.get("exclude_caption_hints", []) or []
        if any(kw.lower() in caption_text for kw in exclude_kws):
            continue # 如果描述裡提到 arm, hand 等，直接跳過這個分類

        exclude_hints = set(cat.get("exclude_caption_hints", []) or [])
        if any(hint.lower() in caption_text for hint in exclude_hints):
            continue
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
    # 修改輸出路徑，現在只輸出一個 json 結果文件
    parser.add_argument("--output-manifest", default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\classify_manifest.json")
    args = parser.parse_args()

    if not os.path.exists(args.aggregated):
        print(f"錯誤: 找不到JSON文件 {args.aggregated}")
        return

    entries = load_json(args.aggregated)
    cfg = load_json(args.config)
    categories = cfg.get("categories", [])
    default_category = cfg.get("default_category", "Unknown")

    print(f"開始分析 {len(entries)} 張圖片的分類...")
    
    manifest = []
    for entry in entries:
        file_stem = entry.get("file", "unknown")
        # 這裡只進行邏輯判斷
        category, reason = classify_one(entry, categories, default_category)
        
        print(f"  [RESULT] {file_stem} -> {category} ({reason})")

        # 只記錄結果，不進行 os.makedirs 或 shutil.copy
        manifest.append({
            "file": file_stem, 
            "category": category,
            "reason": reason
        })

    # 將結果保存為 JSON，供 organizer.py 使用
    os.makedirs(os.path.dirname(args.output_manifest), exist_ok=True)
    with open(args.output_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"All done, New Json at : {args.output_manifest}")
if __name__ == "__main__":
    main()