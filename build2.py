import re
import csv
from pathlib import Path

# 1) Reference folder (your ~500 word videos)
REF_DIR = Path(r"/home/antpc/Downloads/5000_RTH_Videos (Copy)")  # <-- change if needed

# 2) Collected folders (your ~22k videos)
USER_DIRS = {
    "001": [
        Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba/ISL_DATA_USER001/All_clips_USer001_18-01-2026"),
    ],
    "002": [
        Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba/ISL_DATA_USER002/download_2026-01-05_15-16-42/user_002_clips/output_clips"),
        Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba/ISL_DATA_USER002/user002_output_18-01-2026/output_folder"),
        Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba/ISL_DATA_USER002/user002_output_18-01-2026/Skipped_output_folder_user002"),
    ],
    "003": [
        Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba/ISL_DATA_USER003/All_user003_clips_18-01-2026"),
    ],
    "004": [
        Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba/ISL_DATA_USER004/output_clips_20-01-2026_user004"),
    ],
    "005": [
        Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba/ISL_DATA_USER005/All_clips_19-01-2026"),
    ],
}

VIDEO_EXTS = {".mp4", ".mpg", ".mov", ".mkv", ".avi", ".webm"}
OUT_CSV = Path("mapping_1.csv")

def normalize_word(s: str) -> str:
    return s.strip().lower()

def word_from_reference(ref_file: Path) -> str:
    # reference filename is exactly the word: hello.mp4 -> hello
    return normalize_word(ref_file.stem)

def word_from_collected_filename(filename: str) -> str:
    """
    Current rule:
    - Take the first continuous letters at the start of filename stem.
      Examples:
        hello_001.mp4 -> hello
        thankyou-user002-10.mp4 -> thankyou
        school12.mp4 -> school
    """
    stem = Path(filename).stem.lower()

    # remove common prefixes like user001_, u001_, etc.
    stem = re.sub(r"^(user|u)\d{1,3}[_-]*", "", stem)

    m = re.match(r"([a-z]+)", stem)
    return m.group(1) if m else ""

def main():
    # Build reference lookup: word -> ref_path
    ref_lookup = {}
    for f in REF_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            w = word_from_reference(f)
            ref_lookup[w] = str(f)

    if not ref_lookup:
        raise RuntimeError(f"No reference videos found in: {REF_DIR}")

    rows = []
    skipped_no_word = 0
    skipped_no_ref = 0
    total_collected = 0

    for user_id, roots in USER_DIRS.items():
        for root in roots:
            if not root.exists():
                print(f"⚠️ Missing folder for user{user_id}: {root}")
                continue

            for vid in root.rglob("*"):
                if not vid.is_file() or vid.suffix.lower() not in VIDEO_EXTS:
                    continue

                total_collected += 1
                w = word_from_collected_filename(vid.name)
                if not w:
                    skipped_no_word += 1
                    continue

                ref_path = ref_lookup.get(w)
                if not ref_path:
                    skipped_no_ref += 1
                    continue

                rows.append([w, ref_path, str(vid), user_id])

    rows.sort(key=lambda r: (r[0], r[3], r[2]))

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["word", "reference_path", "collected_path", "user_id"])
        writer.writerows(rows)

    print("\n====== SUMMARY ======")
    print(f"Reference words found: {len(ref_lookup)}")
    print(f"Collected videos scanned: {total_collected}")
    print(f"Rows written to mapping.csv: {len(rows)}")
    print(f"Skipped (couldn't extract word from filename): {skipped_no_word}")
    print(f"Skipped (word not found in reference list): {skipped_no_ref}")
    print("=====================\n")

if __name__ == "__main__":
    main()
