import json
import os
import re

# ==================== Path configuration ====================
FLORENCE_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\output\florence"
EXIF_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\exif_results.json"
PADDLE_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\paddle_results.json"
OUTPUT_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\output"


def get_stem(path: str) -> str:
    """
    Extract a stable, extension-free, lowercase "stem" from any path format
    (absolute/relative, forward/back slashes, any extension casing), used
    as the join key across the three JSON sources.

    This is the core bug fix: the original script built the lookup dicts
    keyed on the basename WITH extension (e.g. "test.jpg"), but looked
    them up using a key WITHOUT extension (e.g. "test") -> exif/paddle
    lookups always missed -> filesystem_time_reference_only was always
    empty.
    """
    # Handles both "input\\test.jpg" and "C:\\...\\test.jpg" style paths
    base = re.split(r'[\\/]', path.strip())[-1]
    stem, _ext = os.path.splitext(base)
    return stem.lower()


def pick_primary_year(exif_item: dict, florence_post: dict):
    """
    Priority order: EXIF year > filesystem mtime > filesystem ctime > Florence fallback.

    Florence's own `final_primary_year` is itself derived from filesystem
    time (see physical_metadata.metadata_source == "file_system_created"),
    so it is NOT treated as an independent signal -- it's only used as a
    last resort when neither exif nor local mtime/ctime yield a year.
    `year_source` is returned alongside so downstream consumers (or you,
    debugging) know how trustworthy the year actually is.
    """
    fs_info = exif_item.get("filesystem_time_reference_only", {}) or {}
    mtime = fs_info.get("mtime")
    ctime = fs_info.get("ctime")

    # 1) EXIF original capture time (most trustworthy)
    if exif_item.get("datetime_original"):
        y = exif_item.get("year") or _extract_year(exif_item["datetime_original"])
        if y:
            return y, "exif_datetime_original", mtime, ctime

    # 2) Filesystem mtime
    if mtime:
        y = _extract_year(mtime)
        if y:
            return y, "filesystem_mtime", mtime, ctime

    # 3) Filesystem ctime
    if ctime:
        y = _extract_year(ctime)
        if y:
            return y, "filesystem_ctime", mtime, ctime

    # 4) Florence's own fallback year (also filesystem-derived, last resort only)
    florence_year = (florence_post.get("physical_metadata", {}) or {}).get("metadata_year") \
        or florence_post.get("final_primary_year")
    if florence_year:
        return florence_year, "florence_fallback", mtime, ctime

    return "Unknown", "none", mtime, ctime


def _extract_year(value):
    if not value:
        return None
    m = re.search(r'(\d{4})', str(value))
    return m.group(1) if m else None


def main():
    # ---------- 1. Load exif / paddle, index by stem ----------
    try:
        with open(EXIF_PATH, 'r', encoding='utf-8') as f:
            exif_lookup = {get_stem(i['file']): i for i in json.load(f)}
        with open(PADDLE_PATH, 'r', encoding='utf-8') as f:
            paddle_lookup = {get_stem(i['file']): i for i in json.load(f)}
    except Exception as e:
        print(f"Error loading exif/paddle: {e}")
        return

    all_data = []
    unmatched = []

    # ---------- 2. Iterate over per-image Florence results ----------
    for json_file in sorted(os.listdir(FLORENCE_DIR)):
        if not json_file.endswith(".json"):
            continue

        img_stem = get_stem(json_file)  # already extension-free, lowercase
        with open(os.path.join(FLORENCE_DIR, json_file), 'r', encoding='utf-8') as f:
            flo = json.load(f)

        exif_item = exif_lookup.get(img_stem, {})
        paddle_item = paddle_lookup.get(img_stem, {})

        if not exif_item:
            unmatched.append((img_stem, "exif"))
        if not paddle_item:
            unmatched.append((img_stem, "paddle"))

        florence_post = flo.get("post_processing", {}) or {}
        florence_captions = flo.get("captions", {}) or {}
        florence_ocr_info = florence_post.get("ocr_information", {}) or {}
        florence_obj_stats = florence_post.get("object_statistics", {}) or {}

        # ---- Time signal: exif > mtime > ctime > florence fallback ----
        year, year_source, mtime, ctime = pick_primary_year(exif_item, florence_post)

        # ---- OCR: keep both florence and paddle, don't let one overwrite the other ----
        florence_raw_ocr = flo.get("ocr", {}).get("plain_ocr", {}).get("<OCR>", "")
        paddle_raw_ocr = paddle_item.get("raw_text", "")

        # Paddle's high-confidence (>=0.85) lines tend to be cleaner than
        # Florence's single OCR blob, and matter a lot for classifying
        # "what event / which organization" this photo belongs to.
        paddle_high_conf_lines = [
            line["text"] for line in paddle_item.get("lines", [])
            if line.get("confidence", 0) >= 0.85
        ]

        profile = {
            "file": img_stem,

            "time_info": {
                "inferred_year": year,
                "year_source": year_source,      # exif_datetime_original / filesystem_mtime / filesystem_ctime / florence_fallback / none
                "file_modified_time": mtime,
                "file_created_time": ctime,
                "exif_datetime_original": exif_item.get("datetime_original"),
                "has_exif": bool(exif_item.get("datetime_original")),
                "exif_confidence": exif_item.get("confidence"),
            },

            "visual_description": {
                "short": florence_captions.get("caption", {}).get("<CAPTION>", ""),
                "detailed": florence_captions.get("detailed_caption", {}).get("<DETAILED_CAPTION>", ""),
                "most_detailed": florence_captions.get("more_detailed_caption", {}).get("<MORE_DETAILED_CAPTION>", ""),
            },

            "ocr": {
                "florence_raw": florence_raw_ocr,
                "paddle_raw": paddle_raw_ocr,
                "paddle_high_confidence_lines": paddle_high_conf_lines,
                "keywords": florence_ocr_info.get("keywords", []),
                "possible_years": florence_ocr_info.get("possible_years", []),
                "possible_dates": florence_ocr_info.get("possible_dates", []),
                "possible_times": florence_ocr_info.get("possible_times", []),
                "possible_company": florence_ocr_info.get("possible_company", []),
            },

            "scene_stats": florence_obj_stats,  # e.g. {"human face": 8, "suit": 8, "tie": 2}

            "analysis_notes": (
                "inferred_year follows the priority exif > filesystem_mtime > "
                "filesystem_ctime > florence_fallback and can be used directly "
                "as the classification year; year_source indicates its confidence. "
                "Both florence and paddle OCR outputs are kept side by side -- "
                "paddle is usually more accurate for Chinese / low-contrast text."
            ),
        }

        all_data.append(profile)

    # ---------- 3. Write output ----------
    out_path = os.path.join(OUTPUT_DIR, "aggregated_for_llm.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)

    print(f"Done! Aggregated {len(all_data)} files -> {out_path}")
    if unmatched:
        print(f"WARNING: {len(unmatched)} entries had no match in exif/paddle, check if the file is really missing:")
        for stem, missing_from in unmatched:
            print(f"   - {stem}: missing in {missing_from}")


if __name__ == "__main__":
    main()