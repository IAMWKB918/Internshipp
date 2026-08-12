import argparse
import json
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# 固定輸出資料夾
OUTPUT_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\output"
OUTPUT_FILENAME = "yolo.json"


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


def main():
    parser = argparse.ArgumentParser(description="YOLOv8n 人体检测，输出 yolo.json")
    parser.add_argument("images_dir", help="图片文件夹路径")
    parser.add_argument("--conf", type=float, default=0.35, help="置信度阈值，默认 0.35（想更激进地少漏可以调低，比如 0.25）")
    parser.add_argument("--model", default="yolov8n.pt", help="模型权重，默认最小最快的 yolov8n.pt")
    parser.add_argument("--device", default=None, help="cpu / 0(第一张GPU)，默认自动选")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="yolo.json 输出文件夹，默认写死路径")
    args = parser.parse_args()

    print(f"[1/2] 用 {args.model} 跑 {args.images_dir} 里的图片 (conf>={args.conf}) ...")
    yolo_results = run_yolo(args.images_dir, args.conf, args.model, args.device)

    os.makedirs(args.output_dir, exist_ok=True)
    output_path = os.path.join(args.output_dir, OUTPUT_FILENAME)

    print(f"[2/2] 保存结果到 {output_path} ...")
    save_json(output_path, yolo_results)

    total = len(yolo_results)
    zero_count = sum(1 for v in yolo_results.values() if v["yolo_person_count"] == 0)
    print("-" * 50)
    print(f"总数: {total}")
    print(f"YOLO 判 0 人: {zero_count}")
    print(f"完成: {output_path}")


if __name__ == "__main__":
    main()