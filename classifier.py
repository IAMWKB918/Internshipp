#!/usr/bin/env python3
"""
merge_classifier.py
--------------------
把三路信号统一汇总，按照你的 flowchart 决定 final_primary_year：

  1) EXIF DateTimeOriginal (confidence="high")   -> 直接用，最高优先级
  2) EXIF 缺失 -> 看 OCR：
       2a) Florence OCR 抓到的年份 (英文/数字为主)
       2b) PaddleOCR 抓到的年份 (中文背景板，可选，还没跑就传 None)
       两者都有时，取"更常出现/更近的"那个，可自行调整规则
  3) 文件系统时间 -> 永远不参与这里的决策，只在 exif_analysis 里当参考

florence.py / exif_extractor.py 完全不用改，各自继续产各自的 json。
这个脚本是唯一"做最终判断"的地方，以后加 PaddleOCR 也只改这里。

用法:
  python merge_classifier.py \
      --florence-dir input/florence_results \
      --exif-json exif_results.json \
      --paddle-json paddle_results.json \        # 可选，还没有就不传
      --out-dir merged_results
"""

import argparse
import glob
import json
import os


def load_index_by_basename(json_path):
    """exif_results.json / paddle_results.json 都是 list，且带 file 全路径，
    用 basename 建索引方便跟 florence 的单张结果对上。"""
    if not json_path or not os.path.exists(json_path):
        return {}
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    idx = {}
    for item in data:
        basename = os.path.basename(item.get("file", ""))
        if basename:
            idx[basename] = item
    return idx


def decide_final_year(florence_result: dict, exif_item: dict, paddle_item: dict):
    """
    核心优先级判断，唯一真相来源 (single source of truth)。
    返回 (year, source, confidence)
    """
    # 1) EXIF 高置信度 -> 直接赢
    if exif_item and exif_item.get("confidence") == "high" and exif_item.get("year"):
        return exif_item["year"], "exif", "high"

    # 2) EXIF 缺失 -> 看 OCR (florence 英文/数字 + paddle 中文)
    ocr_info = florence_result.get("post_processing", {}).get("ocr_information", {})
    florence_year = ocr_info.get("ocr_primary_year")

    paddle_year = None
    if paddle_item:
        # 约定 paddle_results.json 每条也带一个 "cn_primary_year" 字段，
        # 具体怎么从 paddleocr 原始输出抽年份，等你那边跑起来后我们再对齐格式。
        paddle_year = paddle_item.get("cn_primary_year")

    if florence_year and paddle_year:
        # 两个 OCR 都抓到年份但不一致时，两个都留底，优先取 paddle
        # (中文背景板通常比英文横幅更靠近拍摄现场，比如活动海报)，
        # 但这个规则你可以按实际情况调整。
        chosen = paddle_year if paddle_year != florence_year else florence_year
        return chosen, "ocr_paddle+florence_conflict" if paddle_year != florence_year else "ocr_agreed", "medium"

    if paddle_year:
        return paddle_year, "ocr_paddle", "medium"

    if florence_year:
        return florence_year, "ocr_florence", "medium"

    # 3) 什么都没有 -> 绝不 fallback 到文件系统时间，标记未解决
    return None, "unresolved", "low"


def merge_one(florence_json_path: str, exif_idx: dict, paddle_idx: dict, out_dir: str):
    with open(florence_json_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    file_path = result.get("image_information", {}).get("file_path", "")
    basename = os.path.basename(file_path) if file_path else os.path.basename(florence_json_path)

    exif_item = exif_idx.get(basename)
    paddle_item = paddle_idx.get(basename)

    year, source, confidence = decide_final_year(result, exif_item, paddle_item)

    pp = result.setdefault("post_processing", {})
    pp["exif_analysis"] = exif_item  # None 就代表这张图完全没跑过 exif_extractor
    pp["paddleocr_analysis"] = paddle_item  # None 就代表还没跑 paddleocr
    pp["final_primary_year"] = year
    pp["final_year_source"] = source
    pp["final_year_confidence"] = confidence
    # 保留旧的 physical_metadata (florence.py 自带那个) 当 legacy 参考，
    # 但它不再参与 final_primary_year 的判断
    if "physical_metadata" in pp:
        pp["physical_metadata"]["note"] = "legacy field, no longer authoritative — see exif_analysis"

    out_path = os.path.join(out_dir, os.path.basename(florence_json_path))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return out_path, year, source, confidence


def main():
    parser = argparse.ArgumentParser(description="EXIF / OCR 多路信号年份判断汇总")
    parser.add_argument("--florence-dir", required=True, help="florence_results 文件夹（每张图一个 json）")
    parser.add_argument("--exif-json", required=True, help="exif_extractor.py 批次输出的 json")
    parser.add_argument("--paddle-json", default=None, help="paddleocr 批次输出的 json（可选，暂时可不传）")
    parser.add_argument("--out-dir", default="merged_results")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    exif_idx = load_index_by_basename(args.exif_json)
    paddle_idx = load_index_by_basename(args.paddle_json)

    florence_files = sorted(glob.glob(os.path.join(args.florence_dir, "*.json")))
    if not florence_files:
        print(f"在 {args.florence_dir} 找不到 florence 的 json 结果")
        return

    stats = {"high": 0, "medium": 0, "low": 0}
    for fp in florence_files:
        out_path, year, source, confidence = merge_one(fp, exif_idx, paddle_idx, args.out_dir)
        stats[confidence] += 1
        print(f"{os.path.basename(fp)} -> year={year}, source={source}, confidence={confidence}")

    print("-" * 50)
    print(f"共处理 {len(florence_files)} 张: high={stats['high']}, medium={stats['medium']}, low={stats['low']}")
    print(f"结果已存到: {args.out_dir}")


if __name__ == "__main__":
    main()