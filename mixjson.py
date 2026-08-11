import argparse
import json
import os
import re

# ==================== Path configuration ====================
FLORENCE_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\output\florence_output"
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
    parser = argparse.ArgumentParser(description="Aggregate Florence results for LLM classification.")
    parser.add_argument("--florence-dir", default=FLORENCE_DIR)
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

def main():
    args = parse_args()

    all_data = []

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

        profile = {
            "file": img_stem,
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
        all_data.append(profile)

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "aggregated_for_llm.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)

    print(f"Aggregation complete. Processed {len(all_data)} files.")

if __name__ == "__main__":
    main()