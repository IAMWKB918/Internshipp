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

def run_yolo(images_dir, conf_threshold, model_name="yolov8n-pose.pt", device=None,
             kpt_conf_threshold=0.5, min_torso_kpts=1, min_face_kpts=2, min_total_kpts=3,
             debug=False, imgsz=1280):
    from ultralytics import YOLO 

    # 強制使用 pose 模型以獲取關鍵點
    if "pose" not in model_name:
        print(f"[info] 檢測到非 Pose 模型，為了判斷肢體碎片，自動切換至 yolov8n-pose.pt")
        model_name = "yolov8n-pose.pt"
        
    model = YOLO(model_name)
    
    files = list_images(images_dir)
    if not files:
        print(f"[warn] 沒在 {images_dir} 找到任何圖片")
        return {}

    results_by_stem = {}
    debug_records = {}  # 只有 debug=True 才會填入，存每一個框(含被濾掉的)的完整診斷資訊
    total = len(files)
    
    # 關鍵點索引定義 (COCO 17點格式)
    # 0:鼻子 1:左眼 2:右眼 3:左耳 4:右耳
    # 5:左肩 6:右肩 11:左髖 12:右髖
    # 7:左肘 8:右肘 9:左腕 10:右腕 13:左膝 14:右膝 15:左踝 16:右踝
    FACE_IDX = [0, 1, 2, 3, 4]        # 五官 -> 用來判斷「有沒有臉」
    TORSO_IDX = [5, 6, 11, 12]        # 肩+髖 -> 用來判斷「有沒有軀幹」（沒看鏡頭/背對也算數）
    ALL_KPT_COUNT = 17

    for i, (stem, path) in enumerate(files, 1):
        try:
            # YOLO Pose 預設就是偵測人
            # imgsz 很關鍵：預設640會把手機原圖壓縮很多，背景遠處的小人物會被壓到偵測不出來
            pred = model.predict(
                source=path,
                conf=conf_threshold,
                imgsz=imgsz,
                verbose=False,
                device=device,
            )[0]
        except Exception as e:
            print(f"[error] {stem}: 推理失敗 ({e})，跳過")
            continue

        h, w = pred.orig_shape[:2]
        img_area = float(h * w) if h and w else 0.0

        # 初始化統計
        raw_person_count = 0     # YOLO 原始偵測到的人數（包含手腳）
        valid_person_count = 0   # 過濾後「有臉/有上半身」的人數
        max_area_ratio = 0.0
        confidences = []
        stem_debug_boxes = [] if debug else None

        # 取得框 (Boxes) 和 關鍵點 (Keypoints)
        boxes = pred.boxes
        keypoints = pred.keypoints

        if boxes is not None and len(boxes) > 0:
            raw_person_count = len(boxes)
            
            # 遍歷每一個偵測到的人物目標
            for idx, box in enumerate(boxes):
                # 取得該目標的 BBox 面積比例
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                area = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
                ratio = (area / img_area) if img_area else 0.0
                box_conf = round(float(box.conf[0]), 4)
                
                # 檢查關鍵點是否存在（用於排除手部/胳膊/腿等局部肢體）
                is_real_portrait = False
                torso_conf_count = face_conf_count = total_conf_count = 0
                if keypoints is not None:
                    # kpt 格式: [17, 3] -> (x, y, conf)
                    kpt = keypoints.data[idx]

                    # 判定邏輯：
                    # A) 軀幹(肩/髖，共4點)至少 min_torso_kpts 個過門檻 + 全身至少 min_total_kpts 個過門檻
                    #    -> 涵蓋「沒看鏡頭/側身/背對」但露出上半身或全身的情況，不要求一定要有臉
                    # B) 或者 臉部(五官，共5點)至少 min_face_kpts 個過門檻
                    #    -> 涵蓋純臉部特寫但軀幹沒入鏡的情況
                    # 純肢體(只有手/腳/手臂/腿，軀幹和臉都偵測不到)兩者皆不成立，會被排除
                    torso_conf_count = sum(
                        1 for j in TORSO_IDX if float(kpt[j][2]) > kpt_conf_threshold
                    )
                    face_conf_count = sum(
                        1 for j in FACE_IDX if float(kpt[j][2]) > kpt_conf_threshold
                    )
                    total_conf_count = sum(
                        1 for j in range(ALL_KPT_COUNT) if float(kpt[j][2]) > kpt_conf_threshold
                    )

                    has_torso = torso_conf_count >= min_torso_kpts and total_conf_count >= min_total_kpts
                    has_face = face_conf_count >= min_face_kpts

                    if has_torso or has_face:
                        is_real_portrait = True

                # 如果判定為有效人像，則計入最終統計
                if is_real_portrait:
                    valid_person_count += 1
                    confidences.append(box_conf)
                    max_area_ratio = max(max_area_ratio, ratio)

                # debug 模式：不管有沒有通過，都記錄這個框的完整診斷資訊
                if debug:
                    stem_debug_boxes.append({
                        "bbox_xyxy": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                        "area_ratio": round(ratio, 4),
                        "box_conf": box_conf,
                        "torso_kpts": torso_conf_count,
                        "face_kpts": face_conf_count,
                        "total_kpts": total_conf_count,
                        "passed": is_real_portrait,
                    })

        results_by_stem[stem] = {
            "yolo_person_count": valid_person_count,        # 過濾後的「真·人數」
            "yolo_raw_person_count": raw_person_count,      # 原始偵測人數（含邊角料）
            "yolo_max_person_area_ratio": round(max_area_ratio, 4),
            "yolo_confidences": confidences,
        }
        if debug:
            debug_records[stem] = {
                "image_size": [w, h],
                "boxes": stem_debug_boxes,
            }

        if i % 20 == 0 or i == total:
            print(f"[{i}/{total}] {stem} -> 有效人數={valid_person_count} (原始={raw_person_count})")

    return results_by_stem, debug_records

def main():
    parser = argparse.ArgumentParser(description="YOLOv8n-Pose 人體關鍵點檢測，過濾肢體邊角料")
    parser.add_argument("images_dir", help="圖片文件夹路徑")
    parser.add_argument("--conf", type=float, default=0.35, help="偵測框置信度閾值")
    parser.add_argument("--model", default="yolov8n-pose.pt", help="模型權重，建議使用 yolov8n-pose.pt")
    parser.add_argument("--device", default=None, help="cpu / 0")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="輸出文件夹")
    parser.add_argument("--kpt-conf", type=float, default=0.5,
                         help="關鍵點置信度門檻，越高越嚴格（預設0.5）")
    parser.add_argument("--min-torso-kpts", type=int, default=1,
                         help="肩+髖(共4點)中至少要幾個超過門檻才算有軀幹（預設1，不要求看到臉，側身/背對也算數）")
    parser.add_argument("--min-face-kpts", type=int, default=2,
                         help="五官(共5點)中至少要幾個超過門檻，用來涵蓋純臉部特寫但軀幹沒入鏡的情況（預設2）")
    parser.add_argument("--min-total-kpts", type=int, default=3,
                         help="全身17個關鍵點中，至少要幾個超過門檻才算真人（預設3，過濾局部肢體的零星雜訊關鍵點）")
    parser.add_argument("--debug", action="store_true",
                         help="輸出詳細偵測資訊(每個框的座標/box信心值/關鍵點統計，含被濾掉的)到 yolo_debug.json，"
                              "方便對照原圖人工核對是否有誤判(例如logo/招牌被當成人)")
    parser.add_argument("--imgsz", type=int, default=1280,
                         help="推論時的圖片邊長(預設1280，原本YOLO內建預設是640)。"
                              "數字越大，背景/遠處的小人物越容易被偵測到，但速度會變慢。"
                              "如果還是漏遠景的人，可以試試 1536 或 1920")
    args = parser.parse_args()

    print(f"[1/2] 使用 Pose 模型 {args.model} 進行偵測 "
          f"(imgsz={args.imgsz}, kpt_conf={args.kpt_conf}, min_torso_kpts={args.min_torso_kpts}, "
          f"min_face_kpts={args.min_face_kpts}, min_total_kpts={args.min_total_kpts})...")
    yolo_results, debug_records = run_yolo(
        args.images_dir, args.conf, args.model, args.device,
        kpt_conf_threshold=args.kpt_conf,
        min_torso_kpts=args.min_torso_kpts,
        min_face_kpts=args.min_face_kpts,
        min_total_kpts=args.min_total_kpts,
        debug=args.debug,
        imgsz=args.imgsz,
    )

    os.makedirs(args.output_dir, exist_ok=True)
    output_path = os.path.join(args.output_dir, OUTPUT_FILENAME)

    print(f"[2/2] 保存結果到 {output_path} ...")
    save_json(output_path, yolo_results)

    if args.debug:
        debug_path = os.path.join(args.output_dir, "yolo_debug.json")
        save_json(debug_path, debug_records)
        print(f"[debug] 逐框診斷資訊已存到 {debug_path}")

        # 額外挑出「信心值貼著 --conf 門檻」的可疑偵測，這種最常是logo/招牌等假陽性
        borderline = []
        for stem, rec in debug_records.items():
            for b in rec["boxes"]:
                if b["passed"] and b["box_conf"] < args.conf + 0.1:
                    borderline.append((stem, b))
        if borderline:
            print(f"[debug] 有 {len(borderline)} 個「信心值貼著門檻(<{args.conf+0.1:.2f})但仍判定為真人」的可疑偵測，建議優先核對：")
            for stem, b in borderline[:30]:
                print(f"    {stem}: bbox={b['bbox_xyxy']} conf={b['box_conf']} "
                      f"torso={b['torso_kpts']} face={b['face_kpts']} total={b['total_kpts']}")

    total = len(yolo_results)
    zero_count = sum(1 for v in yolo_results.values() if v["yolo_person_count"] == 0)
    fragment_count = sum(1 for v in yolo_results.values() if v["yolo_person_count"] == 0 and v["yolo_raw_person_count"] > 0)
    
    print("-" * 50)
    print(f"總處理圖片數: {total}")
    print(f"有效人數為 0 的圖片: {zero_count}")
    print(f"其中包含「純肢體碎片(如手部)」的圖片: {fragment_count}")
    print(f"完成: {output_path}")

if __name__ == "__main__":
    main()