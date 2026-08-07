import json
import os
import shutil
import sys
from pathlib import Path

# --- Force terminal to use UTF-8 for Chinese characters ---
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

# ================= CONFIGURATION =================
MANIFEST_PATH = r"C:\Users\wkb75\Documents\intern cck record\florence\output\sorted\classify_manifest.json"
IMAGES_DIR = r"C:\Users\wkb75\Documents\intern cck record\florence\input"
OUTPUT_ROOT = r"C:\Users\wkb75\Documents\intern cck record\florence\output\organized_photos"
# =================================================

def find_image_file(stem, search_dir):
    search_path = Path(search_dir)
    valid_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.JPG', '.JPEG', '.PNG'}
    
    if not search_path.exists():
        return None

    for file in search_path.rglob('*'):
        if file.is_file() and file.stem == stem and file.suffix in valid_extensions:
            return file
    return None

def main():
    print("="*50)
    print("IMAGE ORGANIZER & RENAMER STARTING")
    print("="*50)
    
    if not os.path.exists(MANIFEST_PATH):
        print(f"ERROR: Manifest file not found!")
        return

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    success_count = 0
    fail_count = 0

    for entry in manifest:
        file_stem = entry.get("file")
        year = str(entry.get("year", "Unknown_Year"))
        category = entry.get("category", "Uncategorized")

        # Step 1: Find the original file
        src_image = find_image_file(file_stem, IMAGES_DIR)
        
        if not src_image:
            print(f"[NOT FOUND] {file_stem}")
            fail_count += 1
            continue

        # Step 2: Create target directory
        target_dir = Path(OUTPUT_ROOT) / year / category
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # Step 3: CONSTRUCT NEW FILENAME (The Rename Part)
        # Format: Year_Category_OriginalName.extension
        new_filename = f"{year}_{category}_{src_image.name}"
        dest_path = target_dir / new_filename

        # Step 4: Copy and Overwrite (No more doubles!)
        try:
            # shutil.copy2 will overwrite if the file exists
            shutil.copy2(src_image, dest_path)
            
            msg = f"[OK] Renamed: {new_filename}"
            print(msg.encode('utf-8', errors='replace').decode('utf-8'))
            success_count += 1
        except Exception as e:
            print(f"[ERROR] Failed to process {src_image.name}: {str(e)}")
            fail_count += 1

    print("="*50)
    print(f"Final Result: {success_count} images organized and renamed.")
    print(f"Check your folder: {OUTPUT_ROOT}")
    print("="*50)

if __name__ == "__main__":
    main()