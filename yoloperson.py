import argparse
import json
import os
import shutil
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_images(images_dir):
    files = []
    for fname in sorted(os.listdir(images_dir)):
        stem, ext = os.path.splitext(fname)
        if ext.lower() in IMAGE_EXTS:
            files.append((stem, os.path.join(images_dir, fname)))
    return files


def run_yolo(images_dir, conf_threshold, model_name="yolov8n.pt", device=None):
    from ultralytics import YOLO  # imported here so --help doesn't require the dep

    model = YOLO(model_name)
    person_class_id = next(k for k, v in model.names.items() if v == "person")

    files = list_images(images_dir)
    if not files:
        print(f"[warn] 没在 {images_dir} 找到任何图片")
        return {}

    results_by_stem = {}
    total = len(files)
    for i, (stem, path) in enumerate(files, 1):
        try:
            pred = model.predict(
                source=path,
                conf=conf_threshold,
                classes=[person_class_id],
                verbose=False,
                device=device,
            )[0]
        except Exception as e:
            print(f"[error] {stem}: 推理失败 ({e})，跳过")
            continue

        h, w = pred.orig_shape[:2]
        img_area = float(h * w) if h and w else 0.0

        boxes = pred.boxes
        person_count = 0
        max_area_ratio = 0.0
        confidences = []
        if boxes is not None and len(boxes) > 0:
            person_count = len(boxes)
            xyxy = boxes.xyxy.tolist()
            confidences = [round(float(c), 4) for c in boxes.conf.tolist()]
            for (x1, y1, x2, y2) in xyxy:
                area = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
                ratio = (area / img_area) if img_area else 0.0
                max_area_ratio = max(max_area_ratio, ratio)

        results_by_stem[stem] = {
            "yolo_person_count": person_count,
            "yolo_max_person_area_ratio": round(max_area_ratio, 4),
            "yolo_confidences": confidences,
        }

        if i % 20 == 0 or i == total:
            print(f"[{i}/{total}] {stem} -> yolo_person_count={person_count}")

    return results_by_stem


def merge(aggregated_entries, yolo_results):
    matched = 0
    # 1. 先把 YOLO 的结果转换成小写 key 的字典，方便匹配
    yolo_results_lower = {k.lower(): v for k, v in yolo_results.items()}
    
    for entry in aggregated_entries:
        raw_file_name = entry.get("file", "")
        # 2. 将 JSON 里的文件名转为小写
        stem_lower = raw_file_name.lower()
        
        # 3. 尝试直接匹配
        y = yolo_results_lower.get(stem_lower)
        
        # 4. 如果没匹配到，且名字里带 _1, _2 这种后缀，尝试去掉后缀再匹配
        if y is None and "_" in stem_lower:
            parts = stem_lower.split("_")
            if parts[-1].isdigit(): # 如果最后一段是数字（如 img_xxx_1 中的 1）
                base_name = "_".join(parts[:-1]) # 变回 img_xxx
                y = yolo_results_lower.get(base_name)

        florence_people = entry.get("num_people_detected", 0) or 0
        
        if y is None:
            entry["yolo_person_count"] = None
            entry["yolo_max_person_area_ratio"] = None
            entry["has_people_union"] = florence_people > 0
            continue
            
        matched += 1
        entry["yolo_person_count"] = y["yolo_person_count"]
        entry["yolo_max_person_area_ratio"] = y["yolo_max_person_area_ratio"]
        entry["yolo_confidences"] = y.get("yolo_confidences", [])
        entry["has_people_union"] = (florence_people > 0) or (y["yolo_person_count"] > 0)
        
    print(f"[merge] {matched}/{len(aggregated_entries)} 条记录成功匹配到 YOLO 结果")
    return aggregated_entries

def main():
    parser = argparse.ArgumentParser(description="YOLOv8n 人体检测，输出并与 aggregated_for_llm.json 合并")
    parser.add_argument("--images-dir", required=True, help="图片文件夹（跟 classifier.py 的 --images-dir 一致）")
    parser.add_argument("--aggregated", required=True, help="现有的 aggregated_for_llm.json 路径")
    parser.add_argument("--output", default=None, help="合并后输出路径，默认覆盖 --aggregated（会先自动备份 .bak）")
    parser.add_argument("--yolo-only-output", default=None, help="可选：只想要 YOLO 单独那份 json 时指定路径")
    parser.add_argument("--conf", type=float, default=0.35, help="置信度阈值，默认 0.35（想更激进地少漏可以调低，比如 0.25）")
    parser.add_argument("--model", default="yolov8n.pt", help="模型权重，默认最小最快的 yolov8n.pt")
    parser.add_argument("--device", default=None, help="cpu / 0(第一张GPU)，默认自动选")
    args = parser.parse_args()

    output_path = args.output or args.aggregated

    print(f"[1/3] 用 {args.model} 跑 {args.images_dir} 里的图片 (conf>={args.conf}) ...")
    yolo_results = run_yolo(args.images_dir, args.conf, args.model, args.device)

    if args.yolo_only_output:
        save_json(args.yolo_only_output, yolo_results)
        print(f"[1/3] YOLO 单独结果已保存: {args.yolo_only_output}")

    print(f"[2/3] 读取 {args.aggregated} 并合并 ...")
    aggregated_entries = load_json(args.aggregated)
    merged = merge(aggregated_entries, yolo_results)

    if output_path == args.aggregated and os.path.exists(args.aggregated):
        backup_path = args.aggregated + ".bak"
        shutil.copy2(args.aggregated, backup_path)
        print(f"[3/3] 已备份原文件: {backup_path}")

    save_json(output_path, merged)
    print(f"[3/3] 合并结果已保存: {output_path}")

    total = len(merged)
    florence_zero = sum(1 for e in merged if (e.get("num_people_detected", 0) or 0) == 0)
    union_zero = sum(1 for e in merged if not e.get("has_people_union", False))
    print("-" * 50)
    print(f"总数: {total}")
    print(f"Florence 判 0 人: {florence_zero}")
    print(f"并集(Florence 或 YOLO)判 0 人: {union_zero}  <- 用这个字段，理论上会比单独 Florence 更少漏人")


if __name__ == "__main__":
    main()