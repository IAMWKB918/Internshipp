import argparse
import json
import os
import shutil
import sys
from collections import defaultdict, Counter
from pathlib import Path

# 处理 Windows 终端中文显示问题
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# 复用 classifier.py 的分类逻辑，保证 organizer 和 classifier 分类结果永远一致，
# 以后只需要改 classifier.py 里的 classify_one，两边就同步生效。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classifier import classify_one  # noqa: E402

# ================= CONFIGURATION (默认值，可按需改这几行) =================
AGGREGATED_JSON_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\aggregated_for_llm.json"
CONFIG_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\config.json"
MANIFEST_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\sorted\classify_manifest.json"
IMAGES_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\input"
OUTPUT_ROOT = r"C:\Users\wkb75\Documents\intern cck record\florence\output\organized_photos"
VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
# =========================================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="读取 aggregated_for_llm.json + config.json，用 classifier.py 的规则分类，"
                     "再按分类顺序编号（如 大合照-1.jpg）复制/归档图片"
    )
    parser.add_argument("--aggregated", default=AGGREGATED_JSON_PATH, help="aggregated_for_llm.json 路径")
    parser.add_argument("--config", default=CONFIG_PATH, help="分类规则 config.json 路径")
    parser.add_argument("--manifest", default=MANIFEST_PATH, help="分类结果写出/读取的 classify_manifest.json 路径")
    parser.add_argument("--images-dir", default=IMAGES_DIR, help="原始图片所在文件夹（会递归搜索子目录）")
    parser.add_argument("--output-root", default=OUTPUT_ROOT, help="归档输出的根目录")
    parser.add_argument("--mode", choices=["copy", "symlink"], default="copy", help="归档方式")
    parser.add_argument("--skip-classify", action="store_true",
                         help="跳过分类步骤，直接读取已有的 --manifest 文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际复制/建链接")
    return parser.parse_args()


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


# ------------------------- 分类部分（直接调用 classifier.classify_one） -------------------------

def apply_cluster_consistency(results, entries, config):
    """同一 visual_cluster_id 的图片按多数投票统一类别；打平票则各自保留原判。

    results: {file: manifest_record_dict}  —— 会被原地修改
    """
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
                results[f]["match_reason"] += "+cluster_override"
                overridden += 1

    if overridden:
        print(f"[CLUSTER] 按 {field} 多数投票，改判了 {overridden} 张图的类别")
    return results


def run_classification(aggregated_path, config_path, manifest_out_path):
    entries = load_json(aggregated_path, "aggregated_for_llm.json")
    config = load_json(config_path, "config.json")
    if entries is None or config is None:
        return None

    categories = config.get("categories", [])
    default_category = config.get("default_category", "Unclassified")

    manifest = []
    for entry in entries:
        file_stem = entry.get("file", "unknown")
        category, reason = classify_one(entry, categories, default_category)
        manifest.append({
            "file": file_stem,
            "category": category,
            "match_reason": reason,
        })
        try:
            print(f"{file_stem} -> {category}  (reason: {reason})")
        except UnicodeEncodeError:
            print(f"{file_stem} -> [category name]  (reason: {reason})")

    # 同 cluster 多数投票统一分类（可选，由 config.json 的 cluster_consistency 控制）
    results_by_file = {r["file"]: r for r in manifest}
    apply_cluster_consistency(results_by_file, entries, config)

    # 分类结果统计，方便肉眼检查
    tally = Counter(r["category"] for r in manifest)
    print("-" * 50)
    print("[CLASSIFY] 分类统计:")
    for cat, count in tally.most_common():
        print(f"    {cat}: {count}")

    # 写出 manifest 备查，也给 --skip-classify 复用
    Path(manifest_out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[CLASSIFY] 已写出 manifest: {manifest_out_path}")

    return manifest


# ------------------------- 归档部分：按分类顺序编号改名 -------------------------

def find_image_file(stem, search_dir):
    """在指定目录及其子目录中查找匹配文件名（不含扩展名）的图片，不区分大小写"""
    search_path = Path(search_dir)
    if not search_path.exists():
        print(f"[ERROR] 搜索路径不存在: {search_dir}")
        return None

    clean_stem = Path(stem).stem.lower()

    for file in search_path.rglob('*'):
        if file.is_file() and file.suffix.lower() in VALID_EXTENSIONS:
            if file.stem.lower() == clean_stem:
                return file
    return None


def sanitize_name(name):
    """把分类名里可能破坏路径/文件名的字符替换掉"""
    return "".join(c if c not in '/\\:*?"<>|' else "_" for c in str(name)).strip() or "Unclassified"


def organize(manifest, images_dir, output_root, mode="copy", dry_run=False):
    success_count = 0
    fail_count = 0
    category_counters = defaultdict(int)  # 每个分类独立计数：大合照-1, 大合照-2 ...

    for entry in manifest:
        raw_file_name = entry.get("file")
        if not raw_file_name:
            continue

        category = sanitize_name(entry.get("category", "Unclassified"))

        src_image = find_image_file(raw_file_name, images_dir)
        if not src_image:
            print(f"[NOT FOUND] 找不到图片: {raw_file_name}")
            entry["status"] = "source image not found, skipped"
            fail_count += 1
            continue

        category_counters[category] += 1
        index = category_counters[category]
        new_filename = f"{category}-{index}{src_image.suffix.lower()}"

        dest_dir = Path(output_root) / category
        dest_path = dest_dir / new_filename

        if dry_run:
            print(f"[DRY-RUN] {src_image} -> {dest_path}")
            entry["status"] = f"dry-run planned: {dest_path}"
            entry["archived_name"] = new_filename
            success_count += 1
            continue

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            if mode == "copy":
                shutil.copy2(src_image, dest_path)
            else:
                if dest_path.exists() or dest_path.is_symlink():
                    dest_path.unlink()
                dest_path.symlink_to(src_image.resolve())
            action = "已归档" if mode == "copy" else "已建立软链接"
            print(f"[OK] {action}: {new_filename}  (源: {src_image.name})")
            entry["status"] = f"{'copied' if mode == 'copy' else 'symlinked'} to {dest_path}"
            entry["archived_name"] = new_filename
            success_count += 1
        except Exception as e:
            print(f"[ERROR] 处理失败 {src_image.name}: {str(e)}")
            entry["status"] = f"error: {e}"
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
        print(f"Aggregated JSON: {args.aggregated}")
        print(f"Config: {args.config}")
        manifest = run_classification(args.aggregated, args.config, args.manifest)
        if manifest is None:
            return

    print("=" * 50)
    print(f"Searching in: {args.images_dir}")
    print("=" * 50)

    success_count, fail_count = organize(
        manifest, args.images_dir, args.output_root, mode=args.mode, dry_run=args.dry_run
    )

    # 归档结果也回写进 manifest，方便核对每张图最终改名成了什么
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    with open(args.manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("=" * 50)
    print(f"整理完成！成功: {success_count}, 失败: {fail_count}")
    print(f"目标位置: {args.output_root}")
    print("=" * 50)


if __name__ == "__main__":
    main()