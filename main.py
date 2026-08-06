import sys
import json
import shutil
from pathlib import Path

from florence import FlorenceAnalyzer
from classifier import load_json, classify
from organizer import unique_destination

# Windows 终端默认编码常常不是 UTF-8，直接打印中文会报错，这里强制改一下
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

INPUT_FOLDER = BASE_DIR / "input"
ANALYSIS_FOLDER = BASE_DIR / "analysis"
OUTPUT_FOLDER = BASE_DIR / "organized"
CONFIG_FILE = BASE_DIR / "config.json"
PIPELINE_LOG_FILE = ANALYSIS_FOLDER / "pipeline_log.json"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


# ============================================================
# FIND IMAGES
# ============================================================

def find_images(folder):
    return sorted(
        f.name for f in Path(folder).iterdir()
        if f.suffix.lower() in IMAGE_EXTENSIONS
    )


# ============================================================
# PROCESS ONE IMAGE (Step 1-4 分析 -> Step 5 分类 -> Step 6 归档)
# ============================================================

def process_image(filename, analyzer, config):
    image_path = INPUT_FOLDER / filename
    json_stem = Path(filename).stem

    print()
    print("=" * 60)
    print(f"Processing: {filename}")
    print("=" * 60)

    log = {
        "filename": filename,
        "status": None,
        "category": None,
        "score": None,
        "destination": None,
        "error": None
    }

    try:
        # --- Step 1-4: Florence 分析 ---
        result = analyzer.analyze_image(str(image_path))
        output_data = {"filename": filename, "analysis": result}

        analysis_json_path = ANALYSIS_FOLDER / f"{json_stem}.json"
        with open(analysis_json_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=4)

        print(f"Analysis saved: {analysis_json_path}")

        # --- Step 5: 分类 ---
        classification = classify(output_data, config)

        classification_json_path = ANALYSIS_FOLDER / f"{json_stem}_classification.json"
        with open(classification_json_path, "w", encoding="utf-8") as f:
            json.dump(classification, f, ensure_ascii=False, indent=4)

        category = classification.get("category", "Other")
        score = classification.get("score", 0)
        print(f"Classification: {category} (score={score})")

        # --- Step 6: 归档（复制原图到 organized/分类名/） ---
        dest_dir = OUTPUT_FOLDER / category
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_path = unique_destination(dest_dir / filename)
        shutil.copy2(image_path, dest_path)

        print(f"Copied to: {dest_path}")

        log.update({
            "status": "done",
            "category": category,
            "score": score,
            "destination": str(dest_path)
        })

    except Exception as e:
        log["status"] = "error"
        log["error"] = str(e)
        print(f"Error processing {filename}: {e}")

    return log


# ============================================================
# MAIN
# ============================================================

def main():
    ANALYSIS_FOLDER.mkdir(exist_ok=True)
    OUTPUT_FOLDER.mkdir(exist_ok=True)

    if not CONFIG_FILE.exists():
        print(f"[ERROR] 找不到 config.json: {CONFIG_FILE}")
        return

    config = load_json(CONFIG_FILE)

    if not INPUT_FOLDER.exists():
        print(f"[ERROR] 找不到 input 文件夹: {INPUT_FOLDER}")
        return

    image_files = find_images(INPUT_FOLDER)

    if not image_files:
        print("No images found in input folder.")
        return

    print(f"Found {len(image_files)} image(s).")

    analyzer = FlorenceAnalyzer()

    logs = []
    for filename in image_files:
        log = process_image(filename, analyzer, config)
        logs.append(log)

    # --- Summary ---
    done = sum(1 for l in logs if l["status"] == "done")
    errors = sum(1 for l in logs if l["status"] == "error")

    print()
    print("=" * 60)
    print("PIPELINE SUMMARY")
    print("=" * 60)
    print(f"完成: {done}")
    print(f"出错: {errors}")

    with open(PIPELINE_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=4)

    print(f"\n流程日志已保存到: {PIPELINE_LOG_FILE}")


if __name__ == "__main__":
    main()