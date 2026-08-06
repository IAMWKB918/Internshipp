import re
import os
import sys
import json
import glob
import logging
import torch
import datetime
import platform
import exifread  # 需先 pip install exifread
from PIL import Image
from transformers import AutoProcessor, AutoModelForCausalLM

# 解决 Windows 终端(cp1252/gbk)打印中文报 UnicodeEncodeError 的问题
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("florence")

MODEL_NAME = "microsoft/Florence-2-base-ft"

# 常见品牌/公司关键词库 —— 可按需扩充
KNOWN_COMPANY_KEYWORDS = [
    "CIMB", "MAYBANK", "PUBLIC BANK", "RHB", "HSBC", "STANDARD CHARTERED",
    "THE EDGE", "BLOOMBERG", "REUTERS", "PWC", "DELOITTE", "KPMG", "EY",
]

# ------------------------------------------------------------------
# 时间/日期/年份相关正则
# ------------------------------------------------------------------
YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}")

DATE_PATTERN = re.compile(
    r"\b(?:"
    r"\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4}"      # 01/03/2024, 01-03-24
    r"|\d{4}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{1,2}"       # 2024-03-01
    r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}"                 # 1 Jan 2024
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}"               # Jan 1, 2024
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?"          # 2024年3月1日
    r")\b",
    re.IGNORECASE
)

# 时间格式：14:30 / 09:15:00 / 2:30 PM / 2PM
TIME_PATTERN = re.compile(
    r"\b(?:"
    r"\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?"
    r"|\d{1,2}\s*[AaPp][Mm]"
    r")\b"
)
# 中文时间格式：上午9点30分 / 下午3时
TIME_CN_PATTERN = re.compile(
    r"(?:上午|下午|晚上|凌晨)?\s*\d{1,2}\s*[点时]\s*(?:\d{1,2}\s*分)?"
)


class FlorenceAnalyzer:
    def __init__(self):
        logger.info("=" * 60)
        logger.info("Loading Florence-2...")
        logger.info("=" * 60)

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.torch_dtype = torch.float16 if self.device == "cuda" else torch.float32

        logger.info(f"Device : {self.device}")
        logger.info(f"DType  : {self.torch_dtype}")
        logger.info(f"Model  : {MODEL_NAME}")

        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME,
            torch_dtype=self.torch_dtype,
            trust_remote_code=True
        ).to(self.device)

        self.processor = AutoProcessor.from_pretrained(
            MODEL_NAME,
            trust_remote_code=True
        )

        self.model.eval()

        logger.info("=" * 60)
        logger.info("Florence-2 Loaded Successfully")
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 元数据提取 (EXIF + 文件属性)
    # ------------------------------------------------------------------
    def _extract_metadata(self, image_path):
        """
        提取图片的物理元数据。
        优先级：EXIF内部日期 > 文件系统创建时间(Created) > 文件系统修改时间(Modified)
        """
        meta_info = {
            "metadata_year": None,
            "metadata_date_full": None,
            "metadata_source": None
        }
        
        try:
            # 1. 尝试读取 EXIF (针对拍照原图)
            with open(image_path, 'rb') as f:
                tags = exifread.process_file(f, details=False)
                for tag in ['EXIF DateTimeOriginal', 'Image DateTime', 'EXIF DateTimeDigitized']:
                    if tag in tags:
                        date_str = str(tags[tag])
                        year_match = YEAR_PATTERN.search(date_str)
                        if year_match:
                            meta_info["metadata_year"] = year_match.group()
                            meta_info["metadata_date_full"] = date_str
                            meta_info["metadata_source"] = "exif_internal"
                            return meta_info

            # 2. 尝试读取文件系统时间 (针对截图或无EXIF图片，即 Properties 面板显示的内容)
            file_stat = os.stat(image_path)
            # Windows 下 st_ctime 是创建时间，Unix 下 st_ctime 是属性改变时间
            if platform.system() == 'Windows':
                timestamp = file_stat.st_ctime
                meta_info["metadata_source"] = "file_system_created"
            else:
                timestamp = file_stat.st_mtime
                meta_info["metadata_source"] = "file_system_modified"
            
            dt_obj = datetime.datetime.fromtimestamp(timestamp)
            meta_info["metadata_year"] = str(dt_obj.year)
            meta_info["metadata_date_full"] = dt_obj.strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            logger.warning(f"Metadata extraction failed for {image_path}: {e}")
            
        return meta_info

    # ------------------------------------------------------------------
    # Core task runner
    # ------------------------------------------------------------------
    def run_task(self, image, task_prompt):
        try:
            inputs = self.processor(
                text=task_prompt,
                images=image,
                return_tensors="pt"
            )

            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            if self.device == "cuda":
                inputs["pixel_values"] = inputs["pixel_values"].to(self.torch_dtype)

            with torch.inference_mode():
                generated_ids = self.model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=2048,
                    num_beams=5,
                    do_sample=False,
                    early_stopping=True,
                    repetition_penalty=1.15,
                    length_penalty=1.0,
                    no_repeat_ngram_size=3
                )

            generated_text = self.processor.batch_decode(
                generated_ids,
                skip_special_tokens=False
            )[0]

            parsed_answer = self.processor.post_process_generation(
                generated_text,
                task=task_prompt,
                image_size=image.size
            )

            return parsed_answer

        except Exception as e:
            logger.warning(f"Task {task_prompt} failed: {e}")
            return {
                "status": "failed",
                "task": task_prompt,
                "error": str(e)
            }

    def safe_task(self, image, task_name, task_prompt):
        logger.info("-" * 60)
        logger.info(task_name)
        logger.info("-" * 60)
        return self.run_task(image, task_prompt)

    # ------------------------------------------------------------------
    # Post-processing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_keywords(raw_tokens):
        seen = set()
        normalized = []
        for token in raw_tokens:
            t = token.strip()
            if not t: continue
            t = re.sub(r"</?s>|<pad>|<unk>", "", t).strip()
            if not t: continue
            if len(t) < 2 and not t.isdigit(): continue
            key = t.lower()
            if key not in seen:
                seen.add(key)
                normalized.append(t)
        return normalized

    @staticmethod
    def _extract_years_from_text(text):
        return sorted(set(YEAR_PATTERN.findall(text) or []))

    @staticmethod
    def _extract_dates_from_text(text):
        return sorted(set(DATE_PATTERN.findall(text) or []))

    @staticmethod
    def _extract_times_from_text(text):
        times = set(TIME_PATTERN.findall(text) or [])
        times |= set(TIME_CN_PATTERN.findall(text) or [])
        return sorted(t.strip() for t in times if t.strip())

    @staticmethod
    def _extract_companies_from_text(text):
        upper_text = text.upper()
        found = []
        for company in KNOWN_COMPANY_KEYWORDS:
            if company in upper_text and company not in found:
                found.append(company)
        return found

    @staticmethod
    def _derive_year_from_dates(date_list):
        derived = set()
        for d in date_list:
            nums = re.findall(r"\d+", d)
            for n in nums:
                if len(n) == 4 and n.startswith(("19", "20")):
                    derived.add(n)
                elif len(n) == 2:
                    yy = int(n)
                    full_year = f"20{n}" if yy <= 30 else f"19{n}"
                    derived.add(full_year)
        return derived

    def extract_ocr_information(self, ocr_result, ocr_region_result=None):
        info = {
            "raw": "",
            "lines": [],
            "keywords": [],
            "ocr_primary_year": None,
            "possible_years": [],
            "possible_dates": [],
            "possible_times": [],
            "possible_company": [],
            "region_labels": []
        }

        try:
            raw = ocr_result.get("<OCR>", "") if isinstance(ocr_result, dict) else ""
            info["raw"] = raw
            words = raw.replace("\n", " ").split()
            info["lines"] = words
            info["keywords"] = self.normalize_keywords(words)
            info["possible_years"] = self._extract_years_from_text(raw)
            info["possible_dates"] = self._extract_dates_from_text(raw)
            info["possible_times"] = self._extract_times_from_text(raw)
            info["possible_company"] = self._extract_companies_from_text(raw)

            if isinstance(ocr_region_result, dict):
                region_data = ocr_region_result.get("<OCR_WITH_REGION>", {})
                labels = region_data.get("labels", [])
                cleaned_labels = self.normalize_keywords(labels)
                info["region_labels"] = cleaned_labels
                region_text = " ".join(cleaned_labels)
                info["possible_years"] = sorted(set(info["possible_years"] + self._extract_years_from_text(region_text)))
                info["possible_dates"] = sorted(set(info["possible_dates"] + self._extract_dates_from_text(region_text)))
                info["possible_times"] = sorted(set(info["possible_times"] + self._extract_times_from_text(region_text)))

            if info["possible_years"]:
                info["ocr_primary_year"] = sorted(info["possible_years"])[-1]
            else:
                derived = self._derive_year_from_dates(info["possible_dates"])
                if derived:
                    info["ocr_primary_year"] = sorted(derived)[-1]
                    info["possible_years"] = sorted(set(info["possible_years"]) | derived)
        except Exception as e:
            logger.warning(f"extract_ocr_information failed: {e}")

        return info

    def combine_caption(self, captions):
        text = []
        try:
            for item in captions.values():
                if isinstance(item, dict):
                    for value in item.values():
                        if isinstance(value, str) and value not in text:
                            text.append(value)
        except: pass
        return {"combined": "\n\n".join(text), "paragraph_count": len(text)}

    def count_detection(self, detection):
        summary = {}
        try:
            labels = detection["<OD>"]["labels"]
            for l in labels: summary[l] = summary.get(l, 0) + 1
        except: pass
        return summary

    def post_process_result(self, result, metadata):
        """
        综合 OCR 和 物理元数据。
        最终 primary_year 优先级：物理元数据年份 > OCR 识别年份
        """
        processed = {}
        processed["combined_caption"] = self.combine_caption(result["captions"])
        ocr_info = self.extract_ocr_information(
            result["ocr"].get("plain_ocr", {}),
            result["ocr"].get("ocr_with_region", {})
        )
        processed["ocr_information"] = ocr_info
        processed["object_statistics"] = self.count_detection(result["detections"]["objects"])
        
        # 核心年份逻辑
        processed["physical_metadata"] = metadata
        processed["final_primary_year"] = metadata["metadata_year"] if metadata["metadata_year"] else ocr_info["ocr_primary_year"]

        result["post_processing"] = processed
        return result

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def analyze_image(self, image_path):
        # 1. 提取物理元数据 (EXIF / Properties)
        metadata = self._extract_metadata(image_path)

        image = Image.open(image_path).convert("RGB")
        width, height = image.size

        result = {
            "image_information": {
                "width": width, "height": height,
                "aspect_ratio": round(width / height, 4),
                "mode": image.mode,
                "file_path": image_path
            },
            "captions": {}, "ocr": {}, "detections": {}, "regions": {}, "metadata": {}
        }

        logger.info("=" * 60)
        logger.info(f"START ANALYSIS: {os.path.basename(image_path)}")
        logger.info("=" * 60)

        result["captions"]["caption"] = self.safe_task(image, "Caption", "<CAPTION>")
        result["captions"]["detailed_caption"] = self.safe_task(image, "Detailed Caption", "<DETAILED_CAPTION>")
        result["captions"]["more_detailed_caption"] = self.safe_task(image, "More Detailed Caption", "<MORE_DETAILED_CAPTION>")
        result["regions"]["dense_region_caption"] = self.safe_task(image, "Dense Region Caption", "<DENSE_REGION_CAPTION>")
        result["regions"]["region_proposal"] = self.safe_task(image, "Region Proposal", "<REGION_PROPOSAL>")
        result["ocr"]["plain_ocr"] = self.safe_task(image, "OCR", "<OCR>")
        result["ocr"]["ocr_with_region"] = self.safe_task(image, "OCR With Region", "<OCR_WITH_REGION>")
        result["detections"]["objects"] = self.safe_task(image, "Object Detection", "<OD>")

        result["metadata"] = {
            "model": MODEL_NAME,
            "device": self.device,
            "torch_dtype": str(self.torch_dtype),
            "executed_tasks": ["CAPTION", "DETAILED_CAPTION", "OCR", "OBJECT_DETECTION"]
        }

        logger.info("=" * 60)
        logger.info("POST PROCESSING (Merging OCR + Metadata)")
        logger.info("=" * 60)

        result = self.post_process_result(result, metadata)

        logger.info(f"FINAL YEAR DETERMINED: {result['post_processing']['final_primary_year']}")
        logger.info("=" * 60)

        return result


# ------------------------------------------------------------------
# CLI 入口
# ------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python florence.py <图片路径 或 图片文件夹>")
        sys.exit(1)

    input_path = sys.argv[1]
    IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

    analyzer = FlorenceAnalyzer()

    if os.path.isdir(input_path):
        image_files = sorted(f for f in glob.glob(os.path.join(input_path, "*")) if f.lower().endswith(IMAGE_EXTS))
        if not image_files:
            print(f"文件夹 {input_path} 内没有找到图片文件"); sys.exit(1)

        output_dir = "C:\\Users\\wkb75\\Documents\\intern cck record\\florence\\output\\florence"
        os.makedirs(output_dir, exist_ok=True)

        for img_path in image_files:
            try:
                output = analyzer.analyze_image(img_path)
                out_name = os.path.splitext(os.path.basename(img_path))[0] + ".json"
                with open(os.path.join(output_dir, out_name), "w", encoding="utf-8") as f:
                    json.dump(output, f, indent=2, ensure_ascii=False)
                logger.info(f"结果已保存: {out_name}")
            except Exception as e:
                logger.warning(f"处理 {img_path} 失败: {e}")
    elif os.path.isfile(input_path):
        output = analyzer.analyze_image(input_path)
        print(json.dumps(output, indent=2, ensure_ascii=False))