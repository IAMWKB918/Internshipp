import argparse
import json
import os
import re

# ==================== Path configuration ====================
FLORENCE_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\output\florence"
EXIF_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\exif_results.json"
PADDLE_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\paddle_results.json"
OUTPUT_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\output"

def parse_args():
    parser = argparse.ArgumentParser(description="Aggregate Florence, EXIF, and PaddleOCR results with enhanced tagging.")
    parser.add_argument("--florence-dir", default=FLORENCE_DIR)
    parser.add_argument("--exif", default=EXIF_PATH)
    parser.add_argument("--paddle", default=PADDLE_PATH)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    return parser.parse_args()

def get_stem(path: str) -> str:
    base = re.split(r'[\\/]', path.strip())[-1]
    stem, _ext = os.path.splitext(base)
    return stem.lower()

def is_gibberish(text):
    """Detects if Florence OCR is hallucinating gibberish characters."""
    if not text: return True
    # Typical Florence hallucination characters for Chinese
    hallucination_pattern = re.compile(r'[艣艪艬艭艴艱艫艼艨艵艿艷艟艂艻艗艘艽艉]')
    return len(hallucination_pattern.findall(text)) > 2

def extract_strict_year(text):
    """
    Extracts 4-digit years (19xx or 20xx) only.
    Uses word boundaries (\b) to ignore numbers like '60' in '60th Anniversary'.
    """
    if not text: return None
    matches = re.findall(r'\b(19\d{2}|20\d{2})\b', str(text))
    return matches[-1] if matches else None

def pick_primary_year(exif_item, paddle_raw, mtime, ctime):
    """
    Decision logic for the most reliable year.
    Priority: EXIF > PaddleOCR Text > File System Time.
    Florence OCR is excluded from year priority due to hallucination risks.
    """
    # 1. Check EXIF
    y = extract_strict_year(exif_item.get("datetime_original"))
    if y: return y, "exif_datetime_original"

    # 2. Check PaddleOCR (Highly reliable for text on image)
    y = extract_strict_year(paddle_raw)
    if y: return y, "paddle_ocr"

    # 3. Check System Times
    if mtime:
        y = extract_strict_year(mtime)
        if y: return y, "filesystem_mtime"
    
    if ctime:
        y = extract_strict_year(ctime)
        if y: return y, "filesystem_ctime"

    return "Unknown", "none"

def main():
    args = parse_args()

    try:
        with open(args.exif, 'r', encoding='utf-8') as f:
            exif_lookup = {get_stem(i['file']): i for i in json.load(f)}
        with open(args.paddle, 'r', encoding='utf-8') as f:
            paddle_lookup = {get_stem(i['file']): i for i in json.load(f)}
    except Exception as e:
        print(f"Error loading source files: {e}"); return

    all_data = []

    for json_file in sorted(os.listdir(args.florence_dir)):
        if not json_file.endswith(".json"): continue

        img_stem = get_stem(json_file)
        with open(os.path.join(args.florence_dir, json_file), 'r', encoding='utf-8') as f:
            flo = json.load(f)

        exif_item = exif_lookup.get(img_stem, {})
        paddle_item = paddle_lookup.get(img_stem, {})
        
        flo_post = flo.get("post_processing", {}) or {}
        flo_captions = flo.get("captions", {}) or {}
        flo_raw_ocr = flo.get("ocr", {}).get("plain_ocr", {}).get("<OCR>", "")
        paddle_raw_ocr = paddle_item.get("raw_text", "")
        
        fs_info = exif_item.get("filesystem_time_reference_only", {}) or {}
        mtime, ctime = fs_info.get("mtime"), fs_info.get("ctime")

        # --- Logic: Strict Year Determination ---
        year, year_source = pick_primary_year(exif_item, paddle_raw_ocr, mtime, ctime)

        # --- Logic: OCR Keyword Filtering ---
        paddle_high_conf = [
            line["text"] for line in paddle_item.get("lines", [])
            if line.get("confidence", 0) >= 0.85
        ]
        
        is_flo_valid = not is_gibberish(flo_raw_ocr)
        flo_keywords = flo_post.get("ocr_information", {}).get("keywords", []) if is_flo_valid else []
        final_keywords = list(set(paddle_high_conf + flo_keywords))

        # ==========================================================
        # NEW TAG 1: has_text_keywords
        # True if PaddleOCR found reliable text
        # ==========================================================
        has_text_keywords = len(paddle_high_conf) > 0

        # ==========================================================
        # NEW TAG 2: visual_cluster_id
        # Placeholder for external clustering results
        # ==========================================================
        visual_cluster_id = flo.get("visual_cluster_id", 0)

        # ==========================================================
        # NEW TAG 3: florence_caption
        # Optimized semantic description for search
        # ==========================================================
        florence_caption = flo_captions.get("more_detailed_caption", {}).get("<MORE_DETAILED_CAPTION>", "")

        profile = {
            "file": img_stem,
            
            # --- New Update Tags ---
            "has_text_keywords": has_text_keywords,
            "visual_cluster_id": visual_cluster_id,
            "florence_caption": florence_caption,

            "time_info": {
                "inferred_year": year,
                "year_source": year_source,
                "file_modified_time": mtime,
                "file_created_time": ctime,
                "exif_datetime_original": exif_item.get("datetime_original"),
            },

            "ocr_summary": {
                "paddle_text": paddle_raw_ocr,
                "florence_ocr_status": "valid" if is_flo_valid else "ignored_gibberish",
                "merged_keywords": final_keywords,
            },

            "scene_stats": flo_post.get("object_statistics", {}),

            "meta": {
                "analysis_note": "Year filtered by strict 4-digit regex. Florence gibberish discarded."
            }
        }
        all_data.append(profile)

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "aggregated_for_llm.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)

    print(f"Aggregation complete. Processed {len(all_data)} files.")

if __name__ == "__main__":
    main()