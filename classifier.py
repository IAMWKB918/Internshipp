import argparse
import json
import os
import sys
import torch
import clip
from PIL import Image

# 强制 UTF-8 环境
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# 全局變量，延遲加載模型
CLIP_MODEL = None
CLIP_PREPROCESS = None
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

def load_clip():
    global CLIP_MODEL, CLIP_PREPROCESS
    if CLIP_MODEL is None:
        print(f"  [System] Loading CLIP model on {DEVICE}...")
        # 使用 ViT-B/32，平衡速度與精度
        CLIP_MODEL, CLIP_PREPROCESS = clip.load("ViT-B/32", device=DEVICE)

def run_clip_verify(image_path, category_name):
    """
    使用 CLIP 判定圖片內容是否真的符合該分類。
    """
    load_clip()
    try:
        image = CLIP_PREPROCESS(Image.open(image_path)).unsqueeze(0).to(DEVICE)
        
        if category_name == "Award_Ceremony":
            # 優化後的 Prompts：讓正向描述更具體，負向描述涵蓋更多日常場景
            text_descriptions = [
                "a photo of people holding a cheque, trophy, certificate, poster, or prize banner", 
                "a photo of people standing or sitting normally without any award or banner",
                "a photo of people holding food, drinks, bags, microphone, tissues,files or mobile phones"
            ]
        else:
            return True, "no_specific_clip_rules"

        text_tokens = clip.tokenize(text_descriptions).to(DEVICE)

        with torch.no_grad():
            logits_per_image, _ = CLIP_MODEL(image, text_tokens)
            probs = logits_per_image.softmax(dim=-1).cpu().numpy().tolist()[0]

        # 判定邏輯：
        # probs[0] 是獎項類的分數
        # 提高閾值到 0.55 或 0.6 可以讓抓取更嚴謹
        is_award = probs[0] > 0.6
        
        status = "CONFIRMED" if is_award else "REJECTED"
        return is_award, f"clip_{status}(pos:{probs[0]:.2f}, neg:{probs[1]+probs[2]:.2f})"
            
    except Exception as e:
        print(f"  [Error] CLIP processing failed for {image_path}: {e}")
        return False, "clip_error"

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_caption_text(entry):
    return " ".join([
        entry.get("caption_short") or "",
        entry.get("caption_detailed") or "",
        entry.get("caption_combined") or "",
    ])

def find_source_file(images_dir, file_stem):
    if not images_dir or not os.path.isdir(images_dir): return None
    for ext in ['.jpg', '.jpeg', '.png', '.JPG', '.PNG', '.JPEG']:
        path = os.path.join(images_dir, file_stem + ext)
        if os.path.exists(path):
            return path
    return None

def classify_one(entry, categories, default_category, images_dir):
    file_name = entry.get("file", "unknown")
    caption_text = get_caption_text(entry).lower()
    
    # --- 基礎人數判定 ---
    florence_count = entry.get("num_people_detected", 0) or 0
    yolo_count = entry.get("yolo_person_count")
    yolo_raw_count = entry.get("yolo_raw_person_count")
    person_count = yolo_count if yolo_count is not None else florence_count

    # 肢體零件過濾邏輯
    limb_kws = ['arm', 'hand', 'finger', 'leg', 'foot', 'thumb', 'wrist', 'elbow', 'nail', 'portion of']
    identity_kws = ['face', 'head', 'portrait', 'man', 'woman', 'lady', 'gentleman', 'boy', 'girl', 'standing', 'sitting', 'posing', 'walking']
    caption_body_part_only = any(kw in caption_text for kw in limb_kws) and not any(kw in caption_text for kw in identity_kws)
    yolo_body_part_only = (yolo_raw_count is not None and yolo_count == 0 and yolo_raw_count > 0)

    if yolo_body_part_only or caption_body_part_only:
        return default_category, "body_part_detected"

    area_ratio = entry.get("yolo_max_person_area_ratio") or entry.get("real_person_max_area_ratio", 0.0)

    # --- 遍歷分類規則 ---
    for cat in categories:
        name = cat["name"]
        
        # 關鍵詞匹配 (增加觸發機率)
        target_kws = cat.get("caption_keywords", []) or cat.get("ocr_keywords", [])
        kw_matched = any(kw.lower() in caption_text for kw in target_kws) if target_kws else True
        
        # 人數與面積閾值判定
        min_p = cat.get("min_person_count")
        max_p = cat.get("max_person_count")
        min_ratio = cat.get("min_real_person_area_ratio")
        
        thresholds_ok = True
        if min_p is not None and person_count < min_p: thresholds_ok = False
        if max_p is not None and person_count > max_p: thresholds_ok = False
        if min_ratio is not None and area_ratio < min_ratio: thresholds_ok = False

        if kw_matched and thresholds_ok:
            # --- CLIP 二次視覺驗證 ---
            if cat.get("use_clip_verify"):
                full_img_path = find_source_file(images_dir, file_name)
                if full_img_path:
                    # 修正後的調用行：
                    is_valid, clip_detail = run_clip_verify(full_img_path, name)
                    if is_valid:
                        return name, clip_detail
                    else:
                        print(f"  [CLIP Skip] {file_name} rejected by CLIP: {clip_detail}")
                        continue # CLIP 判定不是獎項，流向下一個分類規則
                else:
                    print(f"  [Warning] Image not found for CLIP: {file_name}")

            return name, f"rule_matched(count={person_count})"

    return default_category, "no_rule"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--images-dir", default=r"C:\Users\wkb75\Documents\intern cck record\florence\input")
    parser.add_argument("--aggregated", default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\aggregated_for_llm.json")
    parser.add_argument("--config", default=r"C:\Users\wkb75\Documents\intern cck record\florence\config.json")
    parser.add_argument("--output-manifest", default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\classify_manifest.json")
    args = parser.parse_args()

    if not os.path.exists(args.aggregated):
        print(f"錯誤: 找不到JSON文件 {args.aggregated}")
        return

    entries = load_json(args.aggregated)
    cfg = load_json(args.config)
    categories = cfg.get("categories", [])
    default_category = cfg.get("default_category", "Unknown")

    print(f"開始分析 {len(entries)} 張圖片的分類...")
    
    manifest = []
    for entry in entries:
        file_stem = entry.get("file", "unknown")
        category, reason = classify_one(entry, categories, default_category, args.images_dir)
        
        print(f"  [RESULT] {file_stem} -> {category} ({reason})")

        manifest.append({
            "file": file_stem, 
            "category": category,
            "reason": reason
        })

    os.makedirs(os.path.dirname(args.output_manifest), exist_ok=True)
    with open(args.output_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"All done, New Json at : {args.output_manifest}")

if __name__ == "__main__":
    main()