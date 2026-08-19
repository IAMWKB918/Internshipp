import argparse
import json
import os
import re
import sys
import io

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ==================== Path configuration ====================
FLORENCE_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\output\florence"
YOLO_JSON = r"C:\Users\wkb75\Documents\intern cck record\florence\output\yolo.json"
OUTPUT_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\output"

# Heuristic keyword patterns to surface gaze / motion cues that Florence
# does NOT expose as a structured field, but which often show up as free
# text inside the captions (e.g. "...the woman is looking at the camera.")
GAZE_PATTERNS = {
    "looking_at_camera": re.compile(r"looking (at|toward)s? the camera", re.I),
    "looking_away": re.compile(r"looking away|looking (at|toward)s? (something|the side)", re.I),
    "walking_or_passing": re.compile(r"\bwalking\b|\bpassing by\b|\bpassers?[- ]?by\b|\bin the background\b", re.I),
    "posing": re.compile(r"\bposing\b|\bstanding (in front of|together)\b|\bsmiling\b", re.I),
}

def parse_args():
    parser = argparse.ArgumentParser(description="Aggregate Florence + YOLO results for LLM classification.")
    parser.add_argument("--florence-dir", default=FLORENCE_DIR)
    parser.add_argument("--yolo-json", default=YOLO_JSON)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    return parser.parse_args()

def get_stem(path: str) -> str:
    base = re.split(r'[\\/]', path.strip())[-1]
    stem, _ext = os.path.splitext(base)
    return stem.lower()

def extract_caption_hints(*texts):
    """Cheap regex pass over caption text to surface gaze/motion cues,
    since Florence has no dedicated 'looking at camera' / 'passer-by' tag."""
    joined = " ".join(t for t in texts if t)
    return [tag for tag, pattern in GAZE_PATTERNS.items() if pattern.search(joined)]

def summarize_people(object_statistics: dict):
    """Rolls person/man/woman detection counts into one number, since
    scene_type alone is too coarse (only 2 values) for classification."""
    people_keys = ("person", "man", "woman")
    return sum(int(v) for k, v in (object_statistics or {}).items() if k in people_keys)

def load_yolo_results(yolo_json_path):
    if not os.path.exists(yolo_json_path):
        print(f"[warn] YOLO results not found: {yolo_json_path}. Skipping YOLO merge.")
        return {}
    with open(yolo_json_path, 'r', encoding='utf-8') as f:
        yolo_raw = json.load(f)
    return {k.lower(): v for k, v in yolo_raw.items()}

def lookup_yolo(yolo_results_lower, stem_lower):
    y = yolo_results_lower.get(stem_lower)
    if y is None and "_" in stem_lower:
        parts = stem_lower.split("_")
        if parts[-1].isdigit():
            base_name = "_".join(parts[:-1])
            y = yolo_results_lower.get(base_name)
    return y

def main():
    args = parse_args()

    yolo_results_lower = load_yolo_results(args.yolo_json)

    all_data = []
    yolo_matched = 0

    for json_file in sorted(os.listdir(args.florence_dir)):
        if not json_file.endswith(".json"):
            continue

        img_stem = get_stem(json_file)
        try:
            with open(os.path.join(args.florence_dir, json_file), 'r', encoding='utf-8') as f:
                flo = json.load(f)
        except Exception as e:
            print(f"Skipping {json_file}: {e}")
            continue

        image_info = flo.get("image_info", {}) or {}
        flo_captions = flo.get("captions", {}) or {}
        flo_post = flo.get("post_processing", {}) or {}

        caption_short = flo_captions.get("caption", {}).get("<CAPTION>", "")
        caption_detailed = flo_captions.get("more_detailed", {}).get("<MORE_DETAILED_CAPTION>", "")
        caption_combined = flo_post.get("combined_caption", {}).get("combined", "")

        object_statistics = flo_post.get("object_statistics", {}) or {}
        scene_type = flo_post.get("scene_type", "unknown")
        real_person_ratio = flo_post.get("real_person_max_area_ratio", 0.0)

        detected_labels = sorted(object_statistics.keys())
        num_people = summarize_people(object_statistics)
        caption_hints = extract_caption_hints(caption_short, caption_detailed, caption_combined)

        # florence.py tags non-image (video/audio) files with file_type="video"
        # and skips the model entirely for them. Carry that flag through
        # unchanged so classify.py can short-circuit before any of the
        # people-count / caption-keyword logic below runs.
        file_type = flo.get("file_type", "image")
        file_ext = flo.get("file_ext")

        profile = {
            "file": img_stem,
            "file_type": file_type,
            "file_ext": file_ext,
            "image_size": image_info.get("size"),

            "caption_short": caption_short,
            "caption_detailed": caption_detailed,
            "caption_combined": caption_combined,
            "caption_hints": caption_hints,

            "scene_type": scene_type,
            "real_person_max_area_ratio": real_person_ratio,
            "num_people_detected": num_people,
            "object_statistics": object_statistics,
            "detected_labels": detected_labels,
        }

        y = lookup_yolo(yolo_results_lower, img_stem)
        if y is None:
            profile["yolo_person_count"] = None
            profile["yolo_raw_person_count"] = None
            profile["yolo_max_person_area_ratio"] = None
            profile["yolo_confidences"] = []
            profile["has_people_union"] = num_people > 0
            profile["yolo_body_part_only"] = False
        else:
            yolo_matched += 1
            yolo_person_count = y["yolo_person_count"]
            # 新欄位：YOLO 在過濾肢體碎片(手/腳等低信心局部偵測)之前的原始偵測數。
            # raw > 0 但 person_count == 0，代表 YOLO 偵測到東西但判定只是身體局部，不算一個完整的人。
            yolo_raw_person_count = y.get("yolo_raw_person_count", yolo_person_count)
            profile["yolo_person_count"] = yolo_person_count
            profile["yolo_raw_person_count"] = yolo_raw_person_count
            profile["yolo_max_person_area_ratio"] = y["yolo_max_person_area_ratio"]
            profile["yolo_confidences"] = y.get("yolo_confidences", [])
            profile["has_people_union"] = (num_people > 0) or (yolo_person_count > 0)
            profile["yolo_body_part_only"] = (yolo_raw_person_count > 0) and (yolo_person_count == 0)

        all_data.append(profile)

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "aggregated_for_llm.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)

    total = len(all_data)
    florence_zero = sum(1 for e in all_data if (e.get("num_people_detected", 0) or 0) == 0)
    union_zero = sum(1 for e in all_data if not e.get("has_people_union", False))
    body_part_only = sum(1 for e in all_data if e.get("yolo_body_part_only", False))
    video_count = sum(1 for e in all_data if e.get("file_type") == "video")

    print(f"Aggregation complete. Processed {total} files.")
    print(f"Video/audio files detected (file_type='video'): {video_count}  <- will be routed straight to the video category by classify.py")
    print(f"[merge] {yolo_matched}/{total} records successfully matched with YOLO results.")
    print("-" * 50)
    print(f"Total files: {total}")
    print(f"Florence detected 0 people: {florence_zero}")
    print(f"Union (Florence or YOLO) detected 0 people: {union_zero}  <- Use this field to minimize false negatives.")
    print(f"YOLO flagged as body-part-only (raw>0 but filtered person_count=0): {body_part_only}  <- These should NOT land in Individual_Portrait.")
    print(f"Output saved to: {out_path}")

if __name__ == "__main__":
    main()