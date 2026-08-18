import argparse
import json
import os
import re
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

# ── 文字正規化：冠詞 (a/an/the) 忽略 ─────────────────────────────
# 短詞組像「standing on stage」「in front of stage」保留，
# 但 caption 裡「on stage」「on a stage」「on the stage」意思一樣，
# 純字串比對會因為多一個 a/the 就對不上。統一把冠詞拿掉、空白壓平
# 之後再比對 keyword，短詞組就不會再看運氣。
_ARTICLE_RE = re.compile(r'\b(?:a|an|the)\b')

def normalize_text(text):
    t = _ARTICLE_RE.sub(' ', text)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def kw_in_text(keyword, normalized_text):
    """keyword 做同樣的冠詞/空白正規化後，判斷是否為 normalized_text 的「完整詞/詞組」。

    改用 \\b 詞邊界比對，而不是單純子字串比對：
    單字關鍵詞（例如 "ship"、"sign"、"arm"）如果用子字串比對，會誤中
    "championship"、"relationship"、"design"、"army" 這類單字。
    詞組（例如 "on stage"）維持原本行為，只要求詞組本身左右是邊界即可，
    中間的空白仍是逐字比對。
    """
    kw_norm = normalize_text(keyword.lower())
    if not kw_norm:
        return False
    pattern = r'(?<!\w)' + re.escape(kw_norm) + r'(?!\w)'
    return re.search(pattern, normalized_text) is not None

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

def run_clip_verify(image_path, category_name, cat_config):
    """
    使用 CLIP 判定圖片內容是否真的符合該分類。

    Prompts 不再是寫死的整段英文描述句，而是從 config.json 裡該分類的
    clip_positive_keywords / clip_negative_keywords 兩個「詞/短語清單」動態組出來：
        clip_positive_keywords -> "a photo of {keyword}"
        clip_negative_keywords -> "a photo of {keyword}"
    這樣以後要加/減判斷條件（例如把「stage」換成「臺上」的英文描述、
    或加入新的排除情境），只要改 config.json，不用碰程式碼。
    """
    load_clip()
    try:
        image = CLIP_PREPROCESS(Image.open(image_path)).unsqueeze(0).to(DEVICE)

        pos_kws = cat_config.get("clip_positive_keywords", [])
        neg_kws = cat_config.get("clip_negative_keywords", [])

        if not pos_kws:
            # 這個分類沒設定 clip 關鍵詞清單，直接放行（等同舊版的 else 分支）
            return True, "no_specific_clip_rules"

        pos_prompts = [f"a photo of {kw}" for kw in pos_kws]
        neg_prompts = [f"a photo of {kw}" for kw in neg_kws] if neg_kws else \
            ["a photo unrelated to an award or ceremony"]

        text_descriptions = pos_prompts + neg_prompts
        text_tokens = clip.tokenize(text_descriptions).to(DEVICE)

        with torch.no_grad():
            logits_per_image, _ = CLIP_MODEL(image, text_tokens)
            probs = logits_per_image.softmax(dim=-1).cpu().numpy().tolist()[0]

        # 正向清單裡任一詞的分數加總 vs 負向清單分數加總
        pos_score = sum(probs[:len(pos_prompts)])
        neg_score = sum(probs[len(pos_prompts):])

        # 閾值：正向總分 > 0.6 才判定成立，可依實際效果調整
        is_valid = pos_score > 0.6

        status = "CONFIRMED" if is_valid else "REJECTED"
        return is_valid, f"clip_{status}(pos:{pos_score:.2f}, neg:{neg_score:.2f})"

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

def classify_one(entry, categories, default_category, images_dir, video_category=None):
    file_name = entry.get("file", "unknown")

    # --- 最高優先：非圖片檔案（video/audio）直接短路 ---
    # florence.py / mixjson.py 已經在更上游把這種檔案標記為 file_type="video"，
    # 這裡完全跳過人數判定、caption 關鍵詞、CLIP 驗證等所有邏輯，
    # 一律直接歸類到 config.json 裡設定的 video_category。
    if video_category and entry.get("file_type") == "video":
        return video_category, f"video_file_shortcut(ext={entry.get('file_ext')})"

    caption_text = normalize_text(get_caption_text(entry).lower())

    # --- 全域最優先：skip_clip_keywords 強制通過 ---
    # 提到最前面（連人數判定、肢體零件過濾都還沒跑），
    # 因為像 Ship 這種分類，規則是「不管有沒有人、有幾個人，
    # 只要 caption 提到船，就整張圖直接進 Ship」，優先權要蓋過
    # No_People / 肢體零件過濾 / Award_Ceremony 等其他所有判斷。
    for cat in categories:
        name = cat["name"]
        skip_clip_kws = cat.get("skip_clip_keywords", [])
        bypass_hit = next((kw for kw in skip_clip_kws if kw_in_text(kw, caption_text)), None) if skip_clip_kws else None
        if bypass_hit:
            return name, f"strong_keyword_bypass:{bypass_hit}"

    # --- 基礎人數判定 ---
    florence_count = entry.get("num_people_detected", 0) or 0
    yolo_count = entry.get("yolo_person_count")
    yolo_raw_count = entry.get("yolo_raw_person_count")
    person_count = yolo_count if yolo_count is not None else florence_count

    # 肢體零件過濾邏輯
    limb_kws = ['arm', 'hand', 'finger', 'leg', 'foot', 'thumb', 'wrist', 'elbow', 'nail', 'portion of']
    identity_kws = ['face', 'head', 'portrait', 'man', 'woman', 'lady', 'gentleman', 'boy', 'girl', 'standing', 'sitting', 'posing', 'walking']
    caption_body_part_only = any(kw_in_text(kw, caption_text) for kw in limb_kws) and not any(kw_in_text(kw, caption_text) for kw in identity_kws)
    yolo_body_part_only = (yolo_raw_count is not None and yolo_count == 0 and yolo_raw_count > 0)

    if yolo_body_part_only or caption_body_part_only:
        return default_category, "body_part_detected"

    area_ratio = entry.get("yolo_max_person_area_ratio") or entry.get("real_person_max_area_ratio", 0.0)

    # --- 遍歷分類規則 ---
    for cat in categories:
        name = cat["name"]

        # bypass_only 分類（例如 Ship）只透過最上面的全域 skip_clip_keywords
        # 判斷，不參與這裡的一般規則比對。沒有這行的話，Ship 因為沒設
        # caption_keywords / 人數門檻，會被當成「什麼條件都符合」的分類，
        # 排第一個就把後面所有不相關的照片全部吃掉。
        if cat.get("bypass_only"):
            continue

        # 關鍵詞匹配 (增加觸發機率)
        target_kws = cat.get("caption_keywords", []) or cat.get("ocr_keywords", [])
        kw_matched = any(kw_in_text(kw, caption_text) for kw in target_kws) if target_kws else True

        # 排除詞判定：caption 命中排除詞，這個分類整個跳過（不進 CLIP，也不算 match）
        # 用來擋掉像「holding 一支麥克風/樂器/手機/碗盤」誤判成領獎的狀況。
        # 但如果同時也命中 exclude_override_keywords（更強的正向證據，例如真的
        # 有 check/trophy），就不排除，繼續往下判定。
        exclude_kws = cat.get("exclude_keywords", [])
        override_kws = cat.get("exclude_override_keywords", [])
        excluded_hit = next((kw for kw in exclude_kws if kw_in_text(kw, caption_text)), None) if exclude_kws else None
        override_hit = next((kw for kw in override_kws if kw_in_text(kw, caption_text)), None) if override_kws else None

        if excluded_hit and not override_hit:
            print(f"  [Exclude] {file_name} caption matched exclude word '{excluded_hit}' for {name}, skip this category")
            continue
        elif excluded_hit and override_hit:
            print(f"  [Exclude-Override] {file_name} matched exclude '{excluded_hit}' but also override '{override_hit}', keep checking {name}")

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
                    is_valid, clip_detail = run_clip_verify(full_img_path, name, cat)
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
    video_category = cfg.get("video_category")

    print(f"開始分析 {len(entries)} 張圖片的分類...")
    
    manifest = []
    for entry in entries:
        file_stem = entry.get("file", "unknown")
        category, reason = classify_one(entry, categories, default_category, args.images_dir, video_category)
        
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