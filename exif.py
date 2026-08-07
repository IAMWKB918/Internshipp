#!/usr/bin/env python3
"""
exif_extractor.py
------------------
Extracts EXIF DateTimeOriginal for photo date verification.
Improved: Now automatically handles target output directories.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

# Attempt to load dependencies
try:
    import exifread
except ImportError:
    exifread = None

try:
    from PIL import Image
except ImportError:
    Image = None

# Supported image extensions
IMG_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".dng", ".cr2", ".nef",".webp"}


def parse_exif_datetime(raw_str: str) -> Optional[datetime]:
    """Parses EXIF date strings (YYYY:MM:DD HH:MM:SS) into a datetime object."""
    if not raw_str:
        return None
    raw_str = str(raw_str).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw_str, fmt)
        except ValueError:
            continue
    return None


def get_exif_datetime_original(path: Path) -> Tuple[Optional[datetime], Optional[str]]:
    """Extracts DateTimeOriginal using exifread (preferred) or PIL (fallback)."""
    if exifread:
        try:
            with open(path, "rb") as f:
                tags = exifread.process_file(f, details=False, stop_tag="EXIF DateTimeOriginal")
            raw = tags.get("EXIF DateTimeOriginal")
            if raw:
                dt = parse_exif_datetime(str(raw))
                if dt:
                    return dt, "exifread"
        except Exception:
            pass

    if Image:
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                raw = exif.get_ifd(0x8769).get(36867) 
                if raw:
                    dt = parse_exif_datetime(str(raw))
                    if dt:
                        return dt, "PIL"
        except Exception:
            pass
    return None, None


def get_filesystem_time(path: Path) -> Dict[str, str]:
    """Returns filesystem timestamps as references only."""
    try:
        stat = path.stat()
        return {
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "ctime": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        }
    except Exception:
        return {"error": "Could not retrieve filesystem time"}


def process_image(path: Path) -> Dict[str, Any]:
    """Processes a single image file."""
    dt, method = get_exif_datetime_original(path)
    fs_time = get_filesystem_time(path)

    if dt:
        return {
            "file": str(path),
            "confidence": "high",
            "source": "exif",
            "exif_method": method,
            "datetime_original": dt.isoformat(),
            "year": dt.year,
            "filesystem_time_reference_only": fs_time,
            "note": None,
        }
    else:
        return {
            "file": str(path),
            "confidence": "no_exif",
            "source": None,
            "exif_method": None,
            "datetime_original": None,
            "year": None,
            "filesystem_time_reference_only": fs_time,
            "note": "EXIF DateTimeOriginal missing.",
        }


def process_directory(dir_path: Path) -> list:
    """Walks through a directory to find and process images."""
    results = []
    for root, _, files in os.walk(dir_path):
        for name in files:
            file_path = Path(root) / name
            if file_path.suffix.lower() in IMG_EXTS:
                results.append(process_image(file_path))
    return results


def main():
    parser = argparse.ArgumentParser(description="EXIF DateTimeOriginal extraction tool")
    
    parser.add_argument("path", nargs='?', help="Path to a single image or directory")
    parser.add_argument("--file", help="Path to a single image")
    parser.add_argument("--dir", help="Path to a directory")
    # 默认值改为一个文件名，而不是路径
    parser.add_argument("--out", 
                        default=r"C:\Users\wkb75\Documents\intern cck record\florence\output\exif_results.json", 
                        help="Output JSON file")    
    args = parser.parse_args()

    # Determine input path
    input_path_str = args.file or args.dir or args.path
    
    if not input_path_str:
        parser.error("You must specify a file or directory path.")
    
    input_path = Path(input_path_str)

    if not input_path.exists():
        parser.error(f"Path not found: {input_path_str}")

    # --- 核心修复部分 ---
    # 处理输出路径：如果 args.out 是文件夹，则自动拼上默认文件名
    output_path = Path(args.out)
    if output_path.is_dir():
        output_path = output_path / "exif_results.json"
    # ------------------

    # Process
    if input_path.is_file():
        result = process_image(input_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif input_path.is_dir():
        results = process_directory(input_path)
        # 使用处理过的 output_path
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"Processed {len(results)} images. Results saved to: {output_path}")
    else:
        parser.error("Path is neither a file nor a directory.")
        
if __name__ == "__main__":
    main()