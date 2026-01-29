import csv
import sqlite3
import hashlib
import subprocess
import time
import shutil
from pathlib import Path
from typing import Optional, List, Iterator, Tuple, Dict

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

APP_DIR = Path(__file__).parent.resolve()
DB_PATH = APP_DIR / "annotations.db"
MAPPING_CSV = APP_DIR / "mapping.csv"

# ✅ Reference folder
REFERENCE_DIR = Path(r"/home/antpc/Downloads/5000_RTH_Videos (Copy)").resolve()

# ✅ Safe caches (originals untouched)
PREVIEW_DIR = APP_DIR / "previews"
FRAMES_DIR = APP_DIR / "frames_cache"
PREVIEW_DIR.mkdir(exist_ok=True)
FRAMES_DIR.mkdir(exist_ok=True)

# Allowed root paths (security)
ALLOWED_BASE_DIRS = [
    REFERENCE_DIR,
    Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba").resolve(),
]
COLLECTED_BASE = Path(r"/mnt/9a528fe4-4fe8-4dff-9a0c-8b1a3cf3d7ba").resolve()

VIDEO_EXTS = {".mp4", ".mpg", ".mov", ".mkv", ".avi", ".webm"}
CHUNK_SIZE = 1024 * 1024  # 1MB

# Frames fallback settings (moderate quality)
FRAMES_FPS = 12
FRAMES_MAX = 240
FRAMES_HEIGHT = 360
JPG_Q = 5

REFERENCE_MAP: Dict[str, str] = {}
REFERENCE_WORDS: List[str] = []

app = FastAPI(title="ISL Annotation Tool (Login UI + Progress Summary + Export Check)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DB ----------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL,
            reference_path TEXT NOT NULL,
            collected_path TEXT NOT NULL,
            user_id TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_videos_word_user ON videos(word, user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_videos_user ON videos(user_id)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS annotations (
            video_id INTEGER PRIMARY KEY,
            label TEXT NOT NULL CHECK(label IN ('correct','wrong')),
            note TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()

def load_mapping_if_needed():
    if not MAPPING_CSV.exists():
        raise RuntimeError(f"mapping.csv not found at {MAPPING_CSV}")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM videos")
    count = cur.fetchone()["c"]

    if count > 0:
        conn.close()
        return

    with MAPPING_CSV.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [
            (r["word"].strip().lower(), r["reference_path"], r["collected_path"], r["user_id"])
            for r in reader
        ]

    cur.executemany(
        "INSERT INTO videos(word, reference_path, collected_path, user_id) VALUES (?,?,?,?)",
        rows
    )
    conn.commit()
    conn.close()

# ---------------- PATH SAFETY ----------------

def is_allowed_path(p: Path) -> bool:
    try:
        rp = p.resolve()
    except Exception:
        return False
    return any(str(rp).startswith(str(base)) for base in ALLOWED_BASE_DIRS)

def is_collected_path(p: Path) -> bool:
    try:
        rp = p.resolve()
    except Exception:
        return False
    return str(rp).startswith(str(COLLECTED_BASE))

# ---------------- REFERENCE WORDS ----------------

def scan_reference_folder() -> None:
    global REFERENCE_MAP, REFERENCE_WORDS
    if not REFERENCE_DIR.exists():
        raise RuntimeError(f"REFERENCE_DIR not found: {REFERENCE_DIR}")

    ref_map: Dict[str, str] = {}
    for p in REFERENCE_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            w = p.stem.strip().lower()
            if w:
                ref_map[w] = str(p.resolve())

    if not ref_map:
        raise RuntimeError(f"No reference videos found in: {REFERENCE_DIR}")

    REFERENCE_MAP = ref_map
    REFERENCE_WORDS = sorted(ref_map.keys())

def get_reference_words() -> List[str]:
    return REFERENCE_WORDS

def get_reference_path(word: str) -> str:
    w = word.strip().lower()
    if w not in REFERENCE_MAP:
        raise HTTPException(status_code=404, detail=f"Reference video not found for word: {word}")
    return REFERENCE_MAP[w]

# ---------------- PROGRESS HELPERS ----------------

def get_user_word_counts(user_id: str) -> Dict[str, Tuple[int, int]]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT v.word AS word,
               COUNT(*) AS total,
               SUM(CASE WHEN a.video_id IS NOT NULL THEN 1 ELSE 0 END) AS labeled
        FROM videos v
        LEFT JOIN annotations a ON a.video_id = v.id
        WHERE v.user_id = ?
        GROUP BY v.word
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()

    out: Dict[str, Tuple[int, int]] = {}
    for r in rows:
        out[r["word"]] = (int(r["total"]), int(r["labeled"] or 0))
    return out

def word_status(user_id: str, word: str) -> Tuple[str, int, int]:
    """
    Returns status among:
      - no_clips
      - not_started
      - in_progress
      - completed
    """
    counts = get_user_word_counts(user_id)
    total, labeled = counts.get(word, (0, 0))

    if total == 0:
        return "no_clips", total, labeled
    if labeled == 0:
        return "not_started", total, labeled
    if labeled < total:
        return "in_progress", total, labeled
    return "completed", total, labeled

def word_complete_for_export(user_id: str, word: str) -> Tuple[bool, int, int]:
    """
    Export allowed only if total>0 and labeled==total.
    """
    status, total, labeled = word_status(user_id, word)
    return (status == "completed"), total, labeled

# ---------------- PREVIEW (safe; originals untouched) ----------------

def ffprobe_video_codec(p: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nw=1",
                str(p)
            ],
            text=True
        )
        for line in out.splitlines():
            if line.startswith("codec_name="):
                return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None

def preview_path_for(original: Path) -> Path:
    st = original.stat()
    key = f"{original.resolve()}|{st.st_mtime_ns}|{st.st_size}"
    h = hashlib.md5(key.encode("utf-8")).hexdigest()
    return PREVIEW_DIR / f"{h}.mp4"

def run_ffmpeg(cmd: List[str]) -> None:
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        log_file = APP_DIR / "ffmpeg_errors.log"
        with log_file.open("a", encoding="utf-8") as f:
            f.write("\n\n=== FFMPEG FAILED ===\n")
            f.write("CMD: " + " ".join(cmd) + "\n")
            f.write("STDERR:\n" + (p.stderr or "") + "\n")
            f.write("STDOUT:\n" + (p.stdout or "") + "\n")
        tail = "\n".join((p.stderr or "").splitlines()[-25:])
        raise RuntimeError(tail if tail.strip() else "ffmpeg failed")

def ensure_h264_preview(original: Path) -> Path:
    out = preview_path_for(original)

    if out.exists():
        try:
            if out.stat().st_size > 0:
                return out
            out.unlink()
        except Exception:
            pass

    lock = out.with_suffix(".lock")
    if lock.exists():
        for _ in range(240):
            if out.exists() and out.stat().st_size > 0:
                return out
            time.sleep(0.5)

    lock.write_text("1", encoding="utf-8")
    tmp = out.with_suffix(".tmp.mp4")

    vf = "scale=-2:480"
    common_in = [
        "-hide_banner",
        "-fflags", "+genpts",
        "-analyzeduration", "100M",
        "-probesize", "100M",
        "-i", str(original),
    ]
    common_out = [
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-profile:v", "main",
        "-level", "3.1",
        "-movflags", "+faststart",
        str(tmp)
    ]

    cmd1 = ["ffmpeg", "-y", *common_in, "-map", "0:v:0", "-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k", *common_out]
    cmd2 = ["ffmpeg", "-y", *common_in, "-map", "0:v:0", "-an", *common_out]

    try:
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass

        try:
            run_ffmpeg(cmd1)
        except Exception:
            if tmp.exists():
                try: tmp.unlink()
                except Exception: pass
            run_ffmpeg(cmd2)

        if (not tmp.exists()) or tmp.stat().st_size == 0:
            raise RuntimeError("Preview creation failed (empty output).")

        tmp.replace(out)
        return out

    finally:
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
        if lock.exists():
            try: lock.unlink()
            except Exception: pass

# ---------------- SAFE RANGE STREAMING (no 416) ----------------

def parse_range(range_header: str, file_size: int) -> Optional[Tuple[int, int]]:
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header.replace("bytes=", "", 1).strip()
    if "," in spec or "-" not in spec:
        return None

    start_s, end_s = spec.split("-", 1)
    start_s, end_s = start_s.strip(), end_s.strip()

    try:
        if start_s == "":
            length = int(end_s)
            if length <= 0:
                return None
            start = max(file_size - length, 0)
            end = file_size - 1
            return start, end

        start = int(start_s)
        end = file_size - 1 if end_s == "" else int(end_s)

        if start < 0 or end < 0 or start > end or start >= file_size:
            return None
        end = min(end, file_size - 1)
        return start, end
    except Exception:
        return None

def file_iterator(path: Path, start: int, end: int) -> Iterator[bytes]:
    with path.open("rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

# ---------------- FRAMES FALLBACK ----------------

def frames_key_for(original: Path) -> str:
    st = original.stat()
    key = f"{original.resolve()}|{st.st_mtime_ns}|{st.st_size}|frames|{FRAMES_FPS}|{FRAMES_HEIGHT}|{JPG_Q}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()

def ffprobe_duration_seconds(p: Path) -> Optional[float]:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1", str(p)],
            text=True
        )
        for line in out.splitlines():
            if line.startswith("duration="):
                return float(line.split("=", 1)[1].strip())
    except Exception:
        return None
    return None

def ensure_frames(original: Path) -> Tuple[str, int]:
    key = frames_key_for(original)
    out_dir = FRAMES_DIR / key
    out_dir.mkdir(exist_ok=True)

    existing = sorted(out_dir.glob("frame_*.jpg"))
    if existing:
        return key, len(existing)

    lock = out_dir / ".lock"
    if lock.exists():
        for _ in range(240):
            existing = sorted(out_dir.glob("frame_*.jpg"))
            if existing:
                return key, len(existing)
            time.sleep(0.5)

    lock.write_text("1", encoding="utf-8")

    try:
        dur = ffprobe_duration_seconds(original)
        if dur is not None and dur > 0:
            target = int(dur * FRAMES_FPS)
            target = max(1, min(target, FRAMES_MAX))
        else:
            target = FRAMES_MAX

        vf = f"fps={FRAMES_FPS},scale=-2:{FRAMES_HEIGHT}"
        pattern = str(out_dir / "frame_%06d.jpg")

        cmd = [
            "ffmpeg", "-y",
            "-hide_banner",
            "-fflags", "+genpts",
            "-analyzeduration", "100M",
            "-probesize", "100M",
            "-i", str(original),
            "-vf", vf,
            "-frames:v", str(target),
            "-q:v", str(JPG_Q),
            pattern
        ]
        run_ffmpeg(cmd)

        frames = sorted(out_dir.glob("frame_*.jpg"))
        if not frames:
            raise RuntimeError("No frames extracted.")
        return key, len(frames)

    finally:
        if lock.exists():
            try: lock.unlink()
            except Exception: pass

# ---------------- STARTUP ----------------

@app.on_event("startup")
def startup():
    init_db()
    load_mapping_if_needed()
    scan_reference_folder()

# ---------------- UI ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    html = APP_DIR / "index_1.html"
    if not html.exists():
        return HTMLResponse("<h3>index_1.html not found</h3>", status_code=404)
    return HTMLResponse(html.read_text(encoding="utf-8"))

# ---------------- RESET (optional) ----------------

@app.post("/api/reset")
def reset_all(confirm: str = Query(...)):
    if confirm != "YES":
        raise HTTPException(status_code=400, detail="Pass confirm=YES to reset")

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM annotations")
    conn.commit()
    conn.close()

    try:
        if PREVIEW_DIR.exists():
            shutil.rmtree(PREVIEW_DIR)
        PREVIEW_DIR.mkdir(exist_ok=True)

        if FRAMES_DIR.exists():
            shutil.rmtree(FRAMES_DIR)
        FRAMES_DIR.mkdir(exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cache clear failed: {e}")

    return {"ok": True, "message": "Reset done: annotations + caches cleared."}

# ---------------- API ----------------

@app.get("/api/users")
def list_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM videos ORDER BY user_id")
    users = [r["user_id"] for r in cur.fetchall()]
    conn.close()
    return {"users": users, "reference_words": len(REFERENCE_WORDS)}

@app.get("/api/user/{user_id}/words")
def user_words(user_id: str):
    return {
        "user_id": user_id,
        "words": get_reference_words(),
        "total_words": len(get_reference_words())
    }

@app.get("/api/user/{user_id}/summary")
def user_summary(user_id: str):
    words = get_reference_words()
    counts = get_user_word_counts(user_id)

    total_words = len(words)
    words_with_clips = 0
    completed = 0
    started = 0
    no_clips = 0

    per_word = []
    for w in words:
        total, labeled = counts.get(w, (0, 0))
        if total == 0:
            no_clips += 1
            status = "no_clips"
        else:
            words_with_clips += 1
            if labeled == 0:
                status = "not_started"
            elif labeled < total:
                started += 1
                status = "in_progress"
            else:
                completed += 1
                status = "completed"

        per_word.append({"word": w, "total": total, "labeled": labeled, "status": status})

    not_started = max(words_with_clips - started - completed, 0)

    return {
        "user_id": user_id,
        "total_reference_words": total_words,
        "words_with_clips": words_with_clips,
        "completed_words": completed,
        "started_words": started,
        "not_started_words": not_started,
        "no_clip_words": no_clips,
        "per_word": per_word
    }

@app.get("/api/can_export")
def can_export(user_id: str = Query(...), word: str = Query(...)):
    word_l = word.strip().lower()
    complete, total, labeled = word_complete_for_export(user_id, word_l)
    return {"ok": True, "complete": complete, "total": total, "labeled": labeled}

@app.get("/api/user/{user_id}/word/{word}")
def user_word_detail(user_id: str, word: str):
    word_l = word.strip().lower()
    reference_path = get_reference_path(word_l)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT v.id, v.collected_path, v.user_id,
               a.label, a.note, a.updated_at
        FROM videos v
        LEFT JOIN annotations a ON a.video_id = v.id
        WHERE v.user_id = ? AND v.word = ?
        ORDER BY v.collected_path
    """, (user_id, word_l))
    vids = [dict(r) for r in cur.fetchall()]
    conn.close()

    words = get_reference_words()
    idx = words.index(word_l) if word_l in words else 0

    status, total_clips, labeled_clips = word_status(user_id, word_l)

    return {
        "user_id": user_id,
        "word": word_l,
        "reference_path": reference_path,
        "videos": vids,
        "index": idx,
        "total_words": len(words),
        "prev_word": words[idx - 1] if idx > 0 else None,
        "next_word": words[idx + 1] if idx < len(words) - 1 else None,
        "word_total_clips": total_clips,
        "word_labeled_clips": labeled_clips,
        "word_state": status
    }

@app.post("/api/annotate")
def annotate(video_id: int, label: str, note: Optional[str] = None):
    if label not in ("correct", "wrong"):
        raise HTTPException(status_code=400, detail="label must be 'correct' or 'wrong'")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM videos WHERE id = ?", (video_id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="video_id not found")

    cur.execute("""
        INSERT INTO annotations(video_id, label, note)
        VALUES (?,?,?)
        ON CONFLICT(video_id) DO UPDATE SET
            label=excluded.label,
            note=excluded.note,
            updated_at=datetime('now')
    """, (video_id, label, note))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/export.csv")
def export_csv(user_id: str = Query(...), word: str = Query(...)):
    word_l = word.strip().lower()
    complete, total, labeled = word_complete_for_export(user_id, word_l)
    if not complete:
        raise HTTPException(
            status_code=403,
            detail=f"Not ready. Complete this word first. Progress: {labeled}/{total}"
        )

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT v.word, v.user_id, v.reference_path, v.collected_path,
               a.label, a.note, a.updated_at
        FROM videos v
        LEFT JOIN annotations a ON a.video_id = v.id
        ORDER BY v.user_id, v.word, v.collected_path
    """)
    rows = cur.fetchall()
    conn.close()

    def gen():
        import io
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["word","user_id","reference_path","collected_path","label","note","updated_at"])
        yield out.getvalue()
        out.seek(0); out.truncate(0)

        for r in rows:
            writer.writerow([
                r["word"], r["user_id"], r["reference_path"], r["collected_path"],
                r["label"] or "", r["note"] or "", r["updated_at"] or ""
            ])
            yield out.getvalue()
            out.seek(0); out.truncate(0)

    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=annotations_export.csv"}
    )

@app.get("/api/frames")
def frames_api(path: str = Query(...)):
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if p.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="Not a supported video file")
    if not is_allowed_path(p):
        raise HTTPException(status_code=403, detail="Path not allowed")

    key, count = ensure_frames(p)
    return JSONResponse({"key": key, "count": count, "fps": FRAMES_FPS})

@app.get("/frames/{key}/{name}")
def frames_serve(key: str, name: str):
    p = (FRAMES_DIR / key / name).resolve()
    if not str(p).startswith(str((FRAMES_DIR / key).resolve())):
        raise HTTPException(status_code=403, detail="Invalid path")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Frame not found")
    return FileResponse(str(p), media_type="image/jpeg")

@app.get("/media")
def media(request: Request, path: str = Query(...)):
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if p.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="Not a supported video file")
    if not is_allowed_path(p):
        raise HTTPException(status_code=403, detail="Path not allowed")

    codec = ffprobe_video_codec(p)
    serve_path = p

    # collected clips: always preview; references: preview only if not h264
    if is_collected_path(p) or (codec is None) or (codec != "h264"):
        serve_path = ensure_h264_preview(p)

    size = serve_path.stat().st_size
    parsed = parse_range(request.headers.get("range", ""), size)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "video/mp4",
        "Cache-Control": "no-store",
    }

    if parsed is None:
        return StreamingResponse(
            file_iterator(serve_path, 0, size - 1),
            status_code=200,
            headers={**headers, "Content-Length": str(size)}
        )

    start, end = parsed
    headers.update({
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
    })

    return StreamingResponse(
        file_iterator(serve_path, start, end),
        status_code=206,
        headers=headers
    )

