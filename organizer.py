import argparse
import json
import os
import shutil
import sys
from collections import defaultdict, Counter
from pathlib import Path

# 处理 Windows 终端中文显示问题
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

# ================= CONFIGURATION (默认值，可按需改这几行) =================
AGGREGATED_JSON_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\aggregated_for_llm.json"
CONFIG_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\config.json"
MANIFEST_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\sorted\classify_manifest.json"
IMAGES_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\input"
OUTPUT_ROOT = r"C:\Users\wkb75\Documents\intern cck record\florence\output\organized_photos"
# =========================================================================


def parse_args():
    parser = argparse.ArgumentParser(description="读取 aggregated_for_llm.json + config.json 分类，再复制+改名归档图片")
    parser.add_argument("--aggregated-json", default=AGGREGATED_JSON_PATH, help="mixjson 产物 aggregated_for_llm.json 路径")
    parser.add_argument("--config", default=CONFIG_PATH, help="分类规则 config.json 路径")
    parser.add_argument("--manifest", default=MANIFEST_PATH, help="分类结果写出/读取的 classify_manifest.json 路径")
    parser.add_argument("--images-dir", default=IMAGES_DIR, help="原始图片所在文件夹")
    parser.add_argument("--output-root", default=OUTPUT_ROOT, help="归档输出的根目录")
    parser.add_argument("--skip-classify", action="store_true",
                         help="跳过分类步骤，直接读取已有的 --manifest 文件（旧流程兼容）")
    return parser.parse_args()


# ------------------------- 分类部分 -------------------------

def load_json(path, label):
    if not os.path.exists(path):
        print(f"[ERROR] 找不到{label}: {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] 读取{label}失败: {e}")
        return None


def build_ocr_search_text(entry):
    """把 paddle_text 和 merged_keywords 合并成一段大写文本，方便做关键词匹配"""
    ocr = entry.get("ocr_summary", {}) or {}
    paddle_text = ocr.get("paddle_text") or ""
    merged_keywords = ocr.get("merged_keywords") or []
    combined = paddle_text + " " + " ".join(merged_keywords)
    return combined.upper()


def match_category_score(entry, category_cfg, text_gate_enabled):
    """给一个 category 打分：ocr/caption/scene 每命中一条 +1 分"""
    score = 0
    signals = []

    # --- ocr_keywords，受 has_text_keywords 闸门控制 ---
    ocr_kw_list = category_cfg.get("ocr_keywords") or []
    if ocr_kw_list:
        has_text_kw = bool(entry.get("has_text_keywords", False))
        if text_gate_enabled and not has_text_kw:
            pass  # 这张图没有可靠文字，跳过 OCR 匹配
        else:
            search_text = build_ocr_search_text(entry)
            for kw in ocr_kw_list:
                if kw.upper() in search_text:
                    score += 1
                    signals.append(f"ocr:{kw}")

    # --- caption_keywords，对 florence_caption 做子串匹配（大小写不敏感） ---
    caption_kw_list = category_cfg.get("caption_keywords") or []
    if caption_kw_list:
        caption = (entry.get("florence_caption") or "").lower()
        for kw in caption_kw_list:
            if kw.lower() in caption:
                score += 1
                signals.append(f"caption:{kw}")

    # --- scene_objects，看 scene_stats 里有没有这个 key ---
    scene_obj_list = category_cfg.get("scene_objects") or []
    if scene_obj_list:
        stats = entry.get("scene_stats", {}) or {}
        for obj in scene_obj_list:
            if obj in stats:
                score += 1
                signals.append(f"scene:{obj}")

    return score, signals


def classify_entry(entry, config):
    categories = config.get("categories", [])
    text_gate_enabled = bool(config.get("text_gate", {}).get("enabled", False))
    default_category = config.get("default_category", "Unclassified")

    best_name, best_score, best_signals = None, 0, []
    catch_all_candidates = []  # ocr/caption/scene 全空的类别（比如"大合照"），留到最后按人数兜底

    for cat in categories:
        has_rules = cat.get("ocr_keywords") or cat.get("caption_keywords") or cat.get("scene_objects")
        if not has_rules:
            catch_all_candidates.append(cat)
            continue
        score, signals = match_category_score(entry, cat, text_gate_enabled)
        if score > best_score:
            best_name, best_score, best_signals = cat["name"], score, signals

    if best_name:
        return best_name, best_signals

    # 没有任何关键词/caption/scene命中，落到 catch-all 类别按人数判断
    # person 字段经常不准（比如很多张脸但 person 只标 1-2），取 person 和 human face 里较大的那个
    stats = entry.get("scene_stats", {}) or {}
    effective_count = max(stats.get("person", 0), stats.get("human face", 0))
    for cat in catch_all_candidates:
        min_count = cat.get("min_person_count")
        if min_count is not None and effective_count >= min_count:
            return cat["name"], [f"count:{effective_count}>={min_count}"]

    return default_category, []


def apply_cluster_consistency(results, entries, config):
    """同一 visual_cluster_id 的图片按多数投票统一类别；打平票则各自保留原判"""
    cluster_cfg = config.get("cluster_consistency", {})
    if not cluster_cfg.get("enabled", False):
        return results

    field = cluster_cfg.get("field", "visual_cluster_id")
    clusters = defaultdict(list)
    for entry in entries:
        cid = entry.get(field)
        fname = entry.get("file")
        if fname:
            clusters[cid].append(fname)

    overridden = 0
    for cid, files in clusters.items():
        if cid is None or len(files) <= 1:
            continue
        cats = [results[f]["category"] for f in files if f in results]
        if not cats:
            continue
        counter = Counter(cats)
        top_cat, top_count = counter.most_common(1)[0]
        tie = sum(1 for c, n in counter.items() if n == top_count) > 1
        if tie:
            continue  # 打平，各自保留原判
        for f in files:
            if f in results and results[f]["category"] != top_cat:
                results[f]["category"] = top_cat
                overridden += 1

    if overridden:
        print(f"[CLUSTER] 按 visual_cluster_id 多数投票，改判了 {overridden} 张图的类别")
    return results


def run_classification(aggregated_path, config_path, manifest_out_path):
    entries = load_json(aggregated_path, "aggregated_for_llm.json")
    config = load_json(config_path, "config.json")
    if entries is None or config is None:
        return None

    results = {}
    for entry in entries:
        fname = entry.get("file")
        if not fname:
            continue
        year = entry.get("time_info", {}).get("inferred_year", "Unknown_Year")
        category, signals = classify_entry(entry, config)
        results[fname] = {"file": fname, "year": year, "category": category, "_signals": signals}

    results = apply_cluster_consistency(results, entries, config)

    manifest = [{"file": r["file"], "year": r["year"], "category": r["category"]} for r in results.values()]

    # 分类结果统计，方便你肉眼检查
    tally = Counter(r["category"] for r in manifest)
    print("[CLASSIFY] 分类统计:")
    for cat, count in tally.most_common():
        print(f"    {cat}: {count}")

    # 写出 manifest 备查，也给旧流程/skip-classify 复用
    Path(manifest_out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[CLASSIFY] 已写出 manifest: {manifest_out_path}")

    return manifest


# ------------------------- 归档部分（原逻辑不变） -------------------------

def find_image_file(stem, search_dir):
    """
    在指定目录及其子目录中查找匹配文件名的图片，不区分大小写
    """
    search_path = Path(search_dir)
    valid_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}

    if not search_path.exists():
        print(f"[ERROR] 搜索路径不存在: {search_dir}")
        return None

    clean_stem = Path(stem).stem.lower()

    for file in search_path.rglob('*'):
        if file.is_file():
            if file.stem.lower() == clean_stem and file.suffix.lower() in valid_extensions:
                return file
    return None


def organize(manifest, images_dir, output_root):
    success_count = 0
    fail_count = 0

    for entry in manifest:
        raw_file_name = entry.get("file")
        if not raw_file_name:
            continue

        year = str(entry.get("year", "Unknown_Year"))
        category = entry.get("category", "Unclassified")

        src_image = find_image_file(raw_file_name, images_dir)
        if not src_image:
            print(f"[NOT FOUND] 找不到图片: {raw_file_name}")
            fail_count += 1
            continue

        target_dir = Path(output_root) / year / category
        target_dir.mkdir(parents=True, exist_ok=True)

        new_filename = f"{year}_{category}_{src_image.name}"
        dest_path = target_dir / new_filename

        try:
            shutil.copy2(src_image, dest_path)
            print(f"[OK] 已归档: {new_filename}")
            success_count += 1
        except Exception as e:
            print(f"[ERROR] 复制失败 {src_image.name}: {str(e)}")
            fail_count += 1

    return success_count, fail_count


def main():
    args = parse_args()

    print("=" * 50)
    print("IMAGE CLASSIFIER + ORGANIZER STARTING")
    print("=" * 50)

    if args.skip_classify:
        print(f"[SKIP] 跳过分类，直接读取: {args.manifest}")
        manifest = load_json(args.manifest, "classify_manifest.json")
        if manifest is None:
            return
    else:
        print(f"Aggregated JSON: {args.aggregated_json}")
        print(f"Config: {args.config}")
        manifest = run_classification(args.aggregated_json, args.config, args.manifest)
        if manifest is None:
            return

    print("=" * 50)
    print(f"Searching in: {args.images_dir}")
    print("=" * 50)

    success_count, fail_count = organize(manifest, args.images_dir, args.output_root)

    print("=" * 50)
    print(f"整理完成！成功: {success_count}, 失败: {fail_count}")
    print(f"目标位置: {args.output_root}")
    print("=" * 50)


if __name__ == "__main__":
    main()