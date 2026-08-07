import argparse
import json
import os
import shutil
import sys  # 必须导入 sys 模块

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # 兼容旧版本 Python
        pass

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

if __name__ == "__main__":
    main()