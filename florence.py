import re
import os
import sys
import json
import glob
import logging
import torch
import datetime
import platform
import exifread
from PIL import Image
from transformers import AutoProcessor, AutoModelForCausalLM

# Solve Windows terminal encoding issues
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("florence")

MODEL_NAME = "microsoft/Florence-2-base-ft"

CONTAINER_LABELS = [
    "picture frame", "picture", "board", "poster", "television", "monitor",
    "screen", "frame", "photo", "photograph", "portrait", "canvas",
    "wall art", "album", "certificate"
    "banner", "red banner", "flag", "pennant"

]

# If <OD> fails to detect an explicit container box, we fall back to grounding these
# keywords from the caption text itself (the caption already says "a photo of a couple"
# even when OD misses the frame/border object).
PHOTO_CAPTION_KEYWORDS = ["photo", "picture", "poster", "framed", "portrait", "photograph",
    "banner", "award", "certificate"]

class FlorenceAnalyzer:
    def __init__(self):
        logger.info("=" * 60)
        logger.info("Loading Florence-2...")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.torch_dtype = torch.float16 if self.device == "cuda" else torch.float32

        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME, torch_dtype=self.torch_dtype, trust_remote_code=True
        ).to(self.device)
        self.processor = AutoProcessor.from_pretrained(MODEL_NAME, trust_remote_code=True)
        self.model.eval()
        logger.info(f"Florence-2 Loaded Successfully on {self.device}")

    # ------------------------------------------------------------------
    # Spatial Logic: Check if box A (person) is inside box B (frame)
    # ------------------------------------------------------------------
    @staticmethod
    def is_box_inside(inner_box, outer_box, threshold=0.80):
        ix1, iy1, ix2, iy2 = inner_box
        ox1, oy1, ox2, oy2 = outer_box

        x_left = max(ix1, ox1)
        y_top = max(iy1, oy1)
        x_right = min(ix2, ox2)
        y_bottom = min(iy2, oy2)

        if x_right < x_left or y_bottom < y_top:
            return False

        intersection_area = (x_right - x_left) * (y_bottom - y_top)
        inner_area = (ix2 - ix1) * (iy2 - iy1) + 1e-6
        
        return (intersection_area / inner_area) >= threshold

    @staticmethod
    def _overlap_ratio_vs_a(box_a, box_b):
        """Fraction of box_a's area that overlaps box_b. Used for a softer containment
        check against grounded caption-phrase boxes, which are often tighter/looser
        than a true picture-frame box."""
        ax1, ay1, ax2, ay2 = box_a
        bx1, by1, bx2, by2 = box_b
        x_left, y_top = max(ax1, bx1), max(ay1, by1)
        x_right, y_bottom = min(ax2, bx2), min(ay2, by2)
        if x_right < x_left or y_bottom < y_top:
            return 0.0
        intersection = (x_right - x_left) * (y_bottom - y_top)
        area_a = (ax2 - ax1) * (ay2 - ay1) + 1e-6
        return intersection / area_a
        
    def detect_silk_banners(self, image):
        task_prompt = "<CAPTION_TO_PHRASE_GROUNDING>"
        query_text = "red banner, silk banner, award pennant"
    
        inputs = self.processor(text=task_prompt + query_text, images=image, return_tensors="pt")

    def ground_caption_phrases(self, image, caption_text):
        """
        Fallback container detection: re-feed the already-generated caption text into
        <CAPTION_TO_PHRASE_GROUNDING>. This locates where phrases like "a photo of a
        couple" actually sit in the image, even when <OD> failed to output an explicit
        frame/poster/board box. Only called when it's actually needed (see caller).
        """
        task_prompt = "<CAPTION_TO_PHRASE_GROUNDING>"
        try:
            inputs = self.processor(text=task_prompt + caption_text, images=image, return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            if self.device == "cuda":
                inputs["pixel_values"] = inputs["pixel_values"].to(self.torch_dtype)
            with torch.inference_mode():
                generated_ids = self.model.generate(
                    input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                    max_new_tokens=1024, num_beams=3
                )
            generated_text = self.processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
            parsed = self.processor.post_process_generation(generated_text, task=task_prompt, image_size=image.size)
            return parsed.get(task_prompt, {}) or {}
        except Exception as e:
            logger.warning(f"Phrase grounding fallback failed: {e}")
            return {}

    @staticmethod
    def _iou(box_a, box_b):
        ax1, ay1, ax2, ay2 = box_a
        bx1, by1, bx2, by2 = box_b
        x_left, y_top = max(ax1, bx1), max(ay1, by1)
        x_right, y_bottom = min(ax2, bx2), min(ay2, by2)
        if x_right < x_left or y_bottom < y_top:
            return 0.0
        intersection = (x_right - x_left) * (y_bottom - y_top)
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        union = area_a + area_b - intersection + 1e-6
        return intersection / union

    def _dedupe_person_indices(self, person_indices, bboxes, iou_threshold=0.5):
        """
        Florence's <OD> sometimes fires twice on the same physical person (two
        overlapping boxes, one of which may miss the container-containment check
        by a few pixels while the other passes). That single person then gets
        double-counted as both 'real' and 'in photo'. Merge boxes with high mutual
        IoU into one, keeping the larger box, before any real/photo classification.
        """
        kept = []
        # Largest box first so we keep the more complete detection when merging.
        ordered = sorted(person_indices, key=lambda i: -(
            (bboxes[i][2] - bboxes[i][0]) * (bboxes[i][3] - bboxes[i][1])
        ))
        for idx in ordered:
            if any(self._iou(bboxes[idx], bboxes[k]) >= iou_threshold for k in kept):
                continue
            kept.append(idx)
        return kept

    # ------------------------------------------------------------------
    # Refine Labels and Captions
    # ------------------------------------------------------------------
    def refine_detections_and_caption(self, result, image):
        od_result = result["detections"].get("objects", {}).get("<OD>", {})
        caption_text = result["post_processing"]["combined_caption"]["combined"]

        if not od_result or "bboxes" not in od_result:
            return result

        bboxes = od_result["bboxes"]
        labels = od_result["labels"]
        img_w, img_h = result["image_info"]["size"]
        image_area = max(img_w * img_h, 1)

        container_indices = [
            i for i, l in enumerate(labels)
            if any(kw in l.lower() for kw in CONTAINER_LABELS)
        ]
        raw_person_indices = [i for i, l in enumerate(labels) if l.lower() == "person"]
        person_indices = self._dedupe_person_indices(raw_person_indices, bboxes)

        refined_labels = list(labels)
        # Any duplicate person boxes we merged away should not appear as their own
        # "person" entry in the final label statistics either.
        dropped_duplicates = set(raw_person_indices) - set(person_indices)
        for d_idx in dropped_duplicates:
            refined_labels[d_idx] = None  # excluded below when building summary

        real_person_count = 0
        photo_person_count = 0

        # First pass: resolve against OD-detected containers (frame/poster/board/etc.)
        unresolved_person_idx = []
        for p_idx in person_indices:
            p_box = bboxes[p_idx]
            is_in_photo = any(self.is_box_inside(p_box, bboxes[c_idx]) for c_idx in container_indices)
            if is_in_photo:
                refined_labels[p_idx] = "person_in_photo"
                photo_person_count += 1
            else:
                unresolved_person_idx.append(p_idx)

        # Second pass (fallback): only runs when OD left some persons unresolved AND the
        # caption text itself suggests a photo/poster/portrait scene. This is the case
        # your florence.py was missing - OD misses the frame, but the caption already
        # knows it's a photo.
        caption_lower = caption_text.lower()
        caption_suggests_photo = any(kw in caption_lower for kw in PHOTO_CAPTION_KEYWORDS)

        if unresolved_person_idx and caption_suggests_photo:
            grounding = self.ground_caption_phrases(image, caption_text)
            g_bboxes = grounding.get("bboxes", [])
            g_labels = grounding.get("labels", [])

            grounded_container_boxes = [
                g_bboxes[i] for i, l in enumerate(g_labels)
                if any(kw in l.lower() for kw in PHOTO_CAPTION_KEYWORDS)
            ]

            still_unresolved = []
            for p_idx in unresolved_person_idx:
                p_box = bboxes[p_idx]
                # Softer check: most of the person's box falls inside a grounded "photo/picture" phrase box.
                is_in_photo = any(
                    self._overlap_ratio_vs_a(p_box, g_box) >= 0.6 for g_box in grounded_container_boxes
                )
                if is_in_photo:
                    refined_labels[p_idx] = "person_in_photo"
                    photo_person_count += 1
                else:
                    still_unresolved.append(p_idx)
            unresolved_person_idx = still_unresolved

        # Third pass (sibling propagation): a person who is still unresolved but
        # sits tightly overlapping/adjacent to a person who WAS already resolved
        # as "in photo" almost certainly belongs to the same object (e.g. a framed
        # couple portrait where OD/grounding only caught the frame around one of
        # the two people - common when the pair overlaps/touches, like arms around
        # each other). Real people standing that close together essentially never
        # end up half-real/half-photo, so we propagate the classification.
        # Looped (not single-pass) so it also chains across 3+ people crammed into
        # one group photo, not just pairs.
        SIBLING_IOU_THRESHOLD = 0.05
        changed = True
        while changed and unresolved_person_idx:
            changed = False
            resolved_photo_boxes = [
                bboxes[p_idx] for p_idx in person_indices
                if refined_labels[p_idx] == "person_in_photo"
            ]
            if not resolved_photo_boxes:
                break
            still_unresolved = []
            for p_idx in unresolved_person_idx:
                p_box = bboxes[p_idx]
                if any(self._iou(p_box, photo_box) >= SIBLING_IOU_THRESHOLD for photo_box in resolved_photo_boxes):
                    refined_labels[p_idx] = "person_in_photo"
                    photo_person_count += 1
                    changed = True
                else:
                    still_unresolved.append(p_idx)
            unresolved_person_idx = still_unresolved

        real_person_count = len(unresolved_person_idx)

        # Area-ratio signal for downstream classify logic: how big is the largest
        # "real" person box relative to the whole image. A real person genuinely
        # posing for a photo usually takes up a meaningful chunk of the frame; a
        # sliver-sized box left over after containment checks is more often a
        # residual detection artifact than an actual group-photo subject.
        real_person_max_area_ratio = 0.0
        for p_idx in unresolved_person_idx:
            x1, y1, x2, y2 = bboxes[p_idx]
            area_ratio = ((x2 - x1) * (y2 - y1)) / image_area
            real_person_max_area_ratio = max(real_person_max_area_ratio, area_ratio)
        result["post_processing"]["real_person_max_area_ratio"] = round(real_person_max_area_ratio, 4)

        summary = {}
        for l in refined_labels:
            if l is None:
                continue
            summary[l] = summary.get(l, 0) + 1
        result["post_processing"]["object_statistics"] = summary

        # Correct caption based on the final person/photo-person mix
        if photo_person_count > 0 and real_person_count == 0:
            raw_caption = result["post_processing"]["combined_caption"]["combined"]
            fixed_caption = re.sub(r"\bA couple standing\b", "A photo of a couple", raw_caption, flags=re.IGNORECASE)
            fixed_caption = re.sub(r"\bA person standing\b", "A photo of a person", fixed_caption, flags=re.IGNORECASE)
            fixed_caption = re.sub(r"\bThere are people\b", "There is a picture of people", fixed_caption, flags=re.IGNORECASE)
            result["post_processing"]["combined_caption"]["combined"] = fixed_caption
            result["post_processing"]["scene_type"] = "decoration_only_no_real_people"
        elif photo_person_count > 0 and real_person_count > 0:
            result["post_processing"]["scene_type"] = "mixed_real_and_photo_people"
        elif real_person_count > 0:
            result["post_processing"]["scene_type"] = "real_human_present"
        else:
            result["post_processing"]["scene_type"] = "no_people"

        return result

    # ------------------------------------------------------------------
    # Extraction & Analysis Tasks
    # ------------------------------------------------------------------
    def _extract_metadata(self, image_path):
        meta_info = {"metadata_year": None}
        try:
            with open(image_path, 'rb') as f:
                tags = exifread.process_file(f, details=False)
                if 'EXIF DateTimeOriginal' in tags:
                    meta_info["metadata_year"] = str(tags['EXIF DateTimeOriginal'])[:4]
                    return meta_info
            meta_info["metadata_year"] = str(datetime.datetime.fromtimestamp(os.stat(image_path).st_mtime).year)
        except: pass
        return meta_info

    def run_task(self, image, task_prompt):
        inputs = self.processor(text=task_prompt, images=image, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        if self.device == "cuda": inputs["pixel_values"] = inputs["pixel_values"].to(self.torch_dtype)
        
        with torch.inference_mode():
            generated_ids = self.model.generate(
                input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                max_new_tokens=1024, num_beams=3
            )
        generated_text = self.processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
        return self.processor.post_process_generation(generated_text, task=task_prompt, image_size=image.size)

    def analyze_image(self, image_path):
        metadata = self._extract_metadata(image_path)
        image = Image.open(image_path).convert("RGB")
        
        result = {
            "image_info": {"path": image_path, "size": image.size},
            "captions": {}, "detections": {}, "post_processing": {}
        }

        result["captions"]["caption"] = self.run_task(image, "<CAPTION>")
        result["captions"]["more_detailed"] = self.run_task(image, "<MORE_DETAILED_CAPTION>")
        result["detections"]["objects"] = self.run_task(image, "<OD>")

        combined_text = result["captions"]["more_detailed"].get("<MORE_DETAILED_CAPTION>", "")
        result["post_processing"]["combined_caption"] = {"combined": combined_text}
        
        result = self.refine_detections_and_caption(result, image)
        result["post_processing"]["final_year"] = metadata["metadata_year"]
        
        return result

# ------------------------------------------------------------------
# Main Logic
# ------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python script.py <input_folder_or_file>")
        sys.exit(1)

    input_path = sys.argv[1].strip().strip('"')

    # Base output location -> ...\florence\output\florence_output
    base_output_dir = r"C:\Users\wkb75\Documents\intern cck record\florence\output"
    output_dir = sys.argv[2].strip().strip('"') if len(sys.argv) > 2 else os.path.join(base_output_dir, "florence_output")
    os.makedirs(output_dir, exist_ok=True)

    analyzer = FlorenceAnalyzer()

    if os.path.isdir(input_path):
        image_exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
        files = [f for f in glob.glob(os.path.join(input_path, "*")) if f.lower().endswith(image_exts)]
        
        logger.info(f"Processing folder. Saving results to: {output_dir}")
        for img_path in sorted(files):
            try:
                logger.info(f"Analyzing: {os.path.basename(img_path)}")
                output = analyzer.analyze_image(img_path)
                
                out_name = os.path.splitext(os.path.basename(img_path))[0] + ".json"
                with open(os.path.join(output_dir, out_name), "w", encoding="utf-8") as f:
                    json.dump(output, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Failed to process {img_path}: {e}")

    elif os.path.isfile(input_path):
        output = analyzer.analyze_image(input_path)
        out_name = os.path.splitext(os.path.basename(input_path))[0] + ".json"
        with open(os.path.join(output_dir, out_name), "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        logger.info(f"Analysis saved to {output_dir}/{out_name}")
    else:
        logger.error(f"Path not found: {input_path}")