import asyncio
import hashlib
import io
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from pydantic import BaseModel

mimetypes.add_type("font/woff2", ".woff2")

# every household gets its own subdirectory under here: its own tasks.db and
# its own photos folder, resolved per-request from the caller's token. See
# the "multi-tenant" section below for how a household is identified.
DATA_ROOT = os.environ.get("DATA_ROOT", "/data")
REGISTRY_DB_PATH = os.path.join(DATA_ROOT, "registry.db")
HOUSEHOLDS_DIR = os.path.join(DATA_ROOT, "households")

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # raw upload cap for images/audio, before resizing
MAX_VIDEO_BYTES = 200 * 1024 * 1024  # phone video clips are much larger than photos
MAX_PHOTO_DIM = 1600  # longest edge after resize, keeps files phone-friendly
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days — a household app, not a bank

# video/audio are stored as-is (no ffmpeg in this image to transcode or thumbnail),
# so the saved extension must match the real content or browsers won't play it back
MEDIA_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/3gpp": ".3gp",
    "video/x-msvideo": ".avi",
    # .weba (not .webm) for audio — MediaRecorder's voice notes and phone video
    # clips could otherwise collide on the same extension and get served back
    # with the wrong Content-Type
    "audio/webm": ".weba",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/x-m4a": ".m4a",
}


def guess_media_extension(content_type: str) -> str:
    if content_type in MEDIA_EXTENSIONS:
        return MEDIA_EXTENSIONS[content_type]
    return mimetypes.guess_extension(content_type) or ".bin"


# register these explicitly so FileResponse (which infers Content-Type from
# the filename extension) reports them correctly — the platform's built-in
# mimetypes database doesn't reliably know all of these
for _ct, _ext in [
    ("audio/webm", ".weba"), ("audio/ogg", ".ogg"), ("audio/mp4", ".m4a"),
    ("video/3gpp", ".3gp"), ("video/webm", ".webm"), ("video/quicktime", ".mov"),
]:
    mimetypes.add_type(_ct, _ext)

# the container runs on UTC system time, but appointment date/time is entered
# and displayed as UK wall-clock time — always convert through this tzinfo
# (not the container's local time) so reminders land correctly across GMT/BST
UK_TZ = ZoneInfo("Europe/London")
APPOINTMENT_LEAD_OPTIONS = {15, 30, 60, 120, 1440, 2880}  # minutes: 15m,30m,1h,2h,1d,2d

app = FastAPI(title="Shared Tasks")

SECTION_COLORS = ["mustard", "sage", "teal", "clay", "plum"]


# ---------- multi-tenant: households ----------
#
# Each household is a completely separate SQLite file and photos folder
# (see household_db_path/household_photos_dir below) rather than one shared
# database with a household_id column on every table. That means every
# existing query below is untouched — it always operates on "the" database,
# it's just a different physical file per request now. The only new code is
# the plumbing that resolves *which* household a request belongs to and
# routes it to the right files; a lightweight "registry" database is the only
# thing that's actually shared across households, and it holds nothing but
# a household's login code.
#
# Login is a single secret code (e.g. "sunny-otter-4821"), generated at
# signup — it's both the household's identity and its password, same
# single-field feel as the old shared APP_PASSWORD, just one per household
# instead of one for the whole app.

CODE_ADJECTIVES = [
    "sunny", "breezy", "cosy", "tiny", "jolly", "quiet", "brave", "gentle",
    "happy", "lucky", "mighty", "quirky", "sleepy", "spry", "witty", "cheerful",
    "calm", "bold", "bright", "clever", "dapper", "eager", "fuzzy", "giddy",
    "humble", "jazzy", "kindly", "lively", "merry", "nimble",
]
CODE_ANIMALS = [
    "otter", "badger", "fox", "hedgehog", "robin", "sparrow", "rabbit", "squirrel",
    "heron", "puffin", "beaver", "weasel", "magpie", "wren", "swan", "deer",
    "owl", "lynx", "seal", "dolphin", "koala", "panda", "tiger", "falcon",
    "raven", "mole", "vole", "stoat", "curlew", "kestrel",
]


def registry_db() -> sqlite3.Connection:
    os.makedirs(DATA_ROOT, exist_ok=True)
    conn = sqlite3.connect(REGISTRY_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_registry():
    conn = registry_db()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS households (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def all_household_ids() -> list[int]:
    conn = registry_db()
    try:
        return [r["id"] for r in conn.execute("SELECT id FROM households")]
    finally:
        conn.close()


def generate_household_code(conn: sqlite3.Connection) -> str:
    for _ in range(50):
        code = f"{secrets.choice(CODE_ADJECTIVES)}-{secrets.choice(CODE_ANIMALS)}-{secrets.randbelow(9000) + 1000}"
        if not conn.execute("SELECT 1 FROM households WHERE code = ?", (code,)).fetchone():
            return code
    raise HTTPException(500, "Couldn't generate a unique code — try again")


def household_db_path(household_id: int) -> str:
    return os.path.join(HOUSEHOLDS_DIR, str(household_id), "tasks.db")


def household_photos_dir(household_id: int) -> str:
    return os.path.join(HOUSEHOLDS_DIR, str(household_id), "photos")


# ---------- app-wide login gate ----------

def _load_or_create_app_secret() -> str:
    path = os.path.join(DATA_ROOT, "app_secret.txt")
    os.makedirs(DATA_ROOT, exist_ok=True)
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read().strip()
            if existing:
                return existing
    secret = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(secret)
    return secret


APP_SECRET = _load_or_create_app_secret()


def create_app_token(household_id: int) -> str:
    exp = int(time.time()) + TOKEN_TTL_SECONDS
    sig = hashlib.sha256(f"{household_id}:{exp}:{APP_SECRET}".encode()).hexdigest()
    return f"{household_id}.{exp}.{sig}"


def verify_app_token(token: str) -> Optional[int]:
    try:
        hid_str, exp_str, sig = token.split(".", 2)
        household_id = int(hid_str)
        exp = int(exp_str)
        if exp < time.time():
            return None
        expected = hashlib.sha256(f"{household_id}:{exp}:{APP_SECRET}".encode()).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return None
        return household_id
    except Exception:
        return None


def get_household_id(request: Request) -> int:
    token = request.headers.get("x-app-token") or request.query_params.get("token")
    household_id = verify_app_token(token) if token else None
    if household_id is None:
        raise HTTPException(401, "Login required")
    return household_id


protected = APIRouter()


@contextmanager
def db(household_id: int):
    path = household_db_path(household_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_household_db(conn: sqlite3.Connection):
    """Create tables (and run migrations) for one household's database.

    Called once when a household is created, and again for every existing
    household at startup — the same idempotent CREATE-TABLE-IF-NOT-EXISTS /
    column-existence-check pattern as before, just scoped to one household's
    connection instead of a single global one.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            color TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            added_by TEXT NOT NULL,
            done_by TEXT,
            created_at REAL NOT NULL,
            done_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            caption TEXT,
            filename TEXT,
            added_by TEXT NOT NULL,
            created_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            slot TEXT NOT NULL,
            title TEXT NOT NULL,
            added_by TEXT NOT NULL,
            created_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meal_plan (
            weekday INTEGER PRIMARY KEY,
            text TEXT NOT NULL DEFAULT '',
            updated_by TEXT,
            updated_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            lead_minutes INTEGER NOT NULL,
            added_by TEXT NOT NULL,
            reminded INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )"""
    )
    # seed the 7 weekday rows (0=Monday..6=Sunday) on first run
    meal_count = conn.execute("SELECT COUNT(*) c FROM meal_plan").fetchone()["c"]
    if meal_count == 0:
        for wd in range(7):
            conn.execute("INSERT INTO meal_plan (weekday, text) VALUES (?, '')", (wd,))
    # migrate older installs: add password protection column to sections
    section_cols = {c["name"] for c in conn.execute("PRAGMA table_info(sections)")}
    if "password_hash" not in section_cols:
        conn.execute("ALTER TABLE sections ADD COLUMN password_hash TEXT")

    # migrate older installs where filename was NOT NULL (text-only notes need it nullable)
    filename_col = next(
        (c for c in conn.execute("PRAGMA table_info(photos)") if c["name"] == "filename"), None
    )
    if filename_col and filename_col["notnull"]:
        conn.execute("ALTER TABLE photos RENAME TO photos_old")
        conn.execute(
            """CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                caption TEXT,
                filename TEXT,
                added_by TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO photos (id, task_id, caption, filename, added_by, created_at) "
            "SELECT id, task_id, caption, filename, added_by, created_at FROM photos_old"
        )
        conn.execute("DROP TABLE photos_old")

    # migrate older installs: photos can belong directly to a section (an "album"),
    # not just to a task or be a standalone note
    photo_cols = {c["name"] for c in conn.execute("PRAGMA table_info(photos)")}
    if "section_id" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN section_id INTEGER REFERENCES sections(id) ON DELETE CASCADE")

    # migrate older installs: a section used to have one single unnamed album
    # (photos.section_id); replace with named, multiple albums per section
    photo_cols = {c["name"] for c in conn.execute("PRAGMA table_info(photos)")}
    if "album_id" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN album_id INTEGER REFERENCES albums(id) ON DELETE CASCADE")
    legacy_sections = [
        r["section_id"] for r in conn.execute(
            "SELECT DISTINCT section_id FROM photos WHERE section_id IS NOT NULL AND album_id IS NULL"
        )
    ]
    for sid in legacy_sections:
        cur = conn.execute(
            "INSERT INTO albums (section_id, name, sort_order, created_at) VALUES (?,?,?,?)",
            (sid, "Photos", 0, time.time()),
        )
        conn.execute(
            "UPDATE photos SET album_id = ?, section_id = NULL WHERE section_id = ? AND album_id IS NULL",
            (cur.lastrowid, sid),
        )

    # migrate older installs: photos were always images — add media_type so
    # video clips and voice notes can share the same table
    photo_cols = {c["name"] for c in conn.execute("PRAGMA table_info(photos)")}
    if "media_type" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN media_type TEXT NOT NULL DEFAULT 'photo'")

    # migrate older installs: album photos were always shown in upload order —
    # add sort_order so they can be manually reordered, backfilled from the
    # existing chronological order so nothing visibly reshuffles on upgrade
    photo_cols = {c["name"] for c in conn.execute("PRAGMA table_info(photos)")}
    if "sort_order" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        for album_row in conn.execute("SELECT id FROM albums"):
            photo_rows = conn.execute(
                "SELECT id FROM photos WHERE album_id = ? ORDER BY created_at", (album_row["id"],)
            ).fetchall()
            for i, p in enumerate(photo_rows):
                conn.execute("UPDATE photos SET sort_order = ? WHERE id = ?", (i, p["id"]))

    # migrate older installs: videos had no thumbnail (no ffmpeg in the image
    # at the time) — add the column now; existing videos get backfilled by
    # backfill_video_thumbnails() further down, once ffmpeg is available
    photo_cols = {c["name"] for c in conn.execute("PRAGMA table_info(photos)")}
    if "thumb_filename" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN thumb_filename TEXT")

    # migrate older installs: tasks were always shown in (done, created_at)
    # order — add sort_order so they can be manually reordered to prioritise
    # importance, backfilled from that same order so nothing visibly
    # reshuffles on upgrade
    task_cols = {c["name"] for c in conn.execute("PRAGMA table_info(tasks)")}
    if "sort_order" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        for section_row in conn.execute("SELECT id FROM sections"):
            task_rows = conn.execute(
                "SELECT id FROM tasks WHERE section_id = ? ORDER BY done, created_at", (section_row["id"],)
            ).fetchall()
            for i, t in enumerate(task_rows):
                conn.execute("UPDATE tasks SET sort_order = ? WHERE id = ?", (i, t["id"]))

    # seed default sections on first run
    count = conn.execute("SELECT COUNT(*) c FROM sections").fetchone()["c"]
    if count == 0:
        defaults = ["Shopping", "Pet stuff", "Food list", "Holiday"]
        for i, name in enumerate(defaults):
            conn.execute(
                "INSERT INTO sections (name, color, sort_order, created_at) VALUES (?,?,?,?)",
                (name, SECTION_COLORS[i % len(SECTION_COLORS)], i, time.time()),
            )


def init_all_households():
    init_registry()
    os.makedirs(HOUSEHOLDS_DIR, exist_ok=True)
    for hid in all_household_ids():
        with db(hid) as conn:
            init_household_db(conn)


init_all_households()


def log_activity(conn, message: str):
    conn.execute(
        "INSERT INTO activity (message, created_at) VALUES (?, ?)",
        (message, time.time()),
    )


def save_photo_file(raw: bytes, photos_dir: str) -> str:
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except Exception:
        raise HTTPException(400, "That doesn't look like a valid image")
    img.thumbnail((MAX_PHOTO_DIM, MAX_PHOTO_DIM))
    os.makedirs(photos_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.jpg"
    img.save(os.path.join(photos_dir, filename), "JPEG", quality=82, optimize=True)
    return filename


def generate_video_thumbnail(video_filename: str, photos_dir: str) -> Optional[str]:
    """Extract a JPEG frame from a saved video to use as its grid thumbnail.

    Best-effort: returns None (never raises) if ffmpeg fails or times out, so
    a bad/corrupt video never blocks the upload — it just falls back to the
    plain icon tile the frontend already handles.
    """
    video_path = os.path.join(photos_dir, video_filename)
    thumb_filename = f"{uuid.uuid4().hex}_thumb.jpg"
    thumb_path = os.path.join(photos_dir, thumb_filename)
    # seek half a second in first — a frame at 0:00 is often a black/blank
    # flash frame — then fall back to the very first frame for clips too
    # short to seek into
    attempts = [
        ["ffmpeg", "-y", "-ss", "0.5", "-i", video_path, "-frames:v", "1",
         "-vf", "scale='min(480,iw)':-2", thumb_path],
        ["ffmpeg", "-y", "-i", video_path, "-frames:v", "1",
         "-vf", "scale='min(480,iw)':-2", thumb_path],
    ]
    for cmd in attempts:
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
        except (subprocess.SubprocessError, OSError):
            continue
        if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_filename
    return None


def save_media_file(raw: bytes, content_type: str, photos_dir: str) -> tuple[str, str, Optional[str]]:
    """Save an uploaded image/video/audio file, returning (filename, media_type, thumb_filename).

    Images go through the existing resize/recompress path. Video and audio are
    written to disk as-is. Video additionally gets a JPEG thumbnail pulled via
    ffmpeg so it shows a real preview in the grid instead of a bare icon.
    """
    if content_type.startswith("image/"):
        return save_photo_file(raw, photos_dir), "photo", None

    media_type = "video" if content_type.startswith("video/") else "voice"
    ext = guess_media_extension(content_type)
    os.makedirs(photos_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(photos_dir, filename), "wb") as f:
        f.write(raw)
    thumb_filename = generate_video_thumbnail(filename, photos_dir) if media_type == "video" else None
    return filename, media_type, thumb_filename


def backfill_video_thumbnails():
    """One-off catch-up for videos uploaded before ffmpeg was added to the image."""
    for hid in all_household_ids():
        photos_dir = household_photos_dir(hid)
        with db(hid) as conn:
            rows = conn.execute(
                "SELECT id, filename FROM photos "
                "WHERE media_type = 'video' AND filename IS NOT NULL AND thumb_filename IS NULL"
            ).fetchall()
            for r in rows:
                thumb = generate_video_thumbnail(r["filename"], photos_dir)
                if thumb:
                    conn.execute("UPDATE photos SET thumb_filename = ? WHERE id = ?", (thumb, r["id"]))


backfill_video_thumbnails()


def delete_photo_files(filenames, photos_dir: str):
    for fn in filenames:
        if not fn:
            continue
        try:
            os.remove(os.path.join(photos_dir, fn))
        except OSError:
            pass


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000)
    return f"{salt}:{dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, hex_hash = stored.split(":")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000)
        return secrets.compare_digest(dk.hex(), hex_hash)
    except Exception:
        return False


# in-memory: household_id -> {unlock token -> section_id}. Lost on restart,
# which is fine — clients hold tokens only in JS memory too, so a restart
# just means everyone needs to unlock protected lists again. Keyed by
# household first so one household's token can never unlock another's
# same-numbered section.
UNLOCK_TOKENS: dict[int, dict[str, int]] = {}


def require_unlocked(conn, household_id: int, section_id: int, token: Optional[str]):
    row = conn.execute("SELECT password_hash FROM sections WHERE id = ?", (section_id,)).fetchone()
    if row and row["password_hash"]:
        household_tokens = UNLOCK_TOKENS.get(household_id, {})
        if not token or household_tokens.get(token) != section_id:
            raise HTTPException(403, "This list is locked")


# ---------- schemas ----------

class SectionCreate(BaseModel):
    name: str


class SectionUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


class SectionMove(BaseModel):
    direction: str  # "up" or "down"


class SectionProtection(BaseModel):
    current_password: Optional[str] = None
    new_password: Optional[str] = None  # empty/None = remove protection


class SectionUnlock(BaseModel):
    password: str


class TaskCreate(BaseModel):
    section_id: int
    text: str
    added_by: str
    unlock_token: Optional[str] = None


class TaskUpdate(BaseModel):
    done: bool
    by: str
    unlock_token: Optional[str] = None


class TaskTextUpdate(BaseModel):
    text: str
    by: str
    unlock_token: Optional[str] = None


class TaskBulkCreate(BaseModel):
    section_id: int
    texts: list[str]
    added_by: str
    unlock_token: Optional[str] = None


class TaskReorder(BaseModel):
    task_ids: list[int]
    unlock_token: Optional[str] = None


class AlbumCreate(BaseModel):
    section_id: int
    name: str
    unlock_token: Optional[str] = None


class AlbumUpdate(BaseModel):
    name: str
    unlock_token: Optional[str] = None


class AlbumMove(BaseModel):
    direction: str  # "up" or "down"
    unlock_token: Optional[str] = None


class AlbumReorder(BaseModel):
    photo_ids: list[int]
    unlock_token: Optional[str] = None


class MealPlanUpdate(BaseModel):
    text: str
    updated_by: str


class PhotoUpdate(BaseModel):
    caption: str
    unlock_token: Optional[str] = None


class PhotoMove(BaseModel):
    album_id: int
    unlock_token: Optional[str] = None  # unlocks the photo's current (source) album
    dest_unlock_token: Optional[str] = None  # unlocks the destination album


class AppointmentCreate(BaseModel):
    name: str
    date: str  # 'YYYY-MM-DD'
    time: str  # 'HH:MM'
    lead_minutes: int
    added_by: str


class AppointmentUpdate(BaseModel):
    name: str
    date: str
    time: str
    lead_minutes: int


class EventCreate(BaseModel):
    date: str  # 'YYYY-MM-DD'
    slot: str  # 'day' or 'evening'
    title: str
    added_by: str


class LoginRequest(BaseModel):
    code: str


# ---------- routes ----------

@app.post("/api/households")
def create_household():
    reg = registry_db()
    try:
        code = generate_household_code(reg)
        cur = reg.execute("INSERT INTO households (code, created_at) VALUES (?, ?)", (code, time.time()))
        reg.commit()
        new_id = cur.lastrowid
    finally:
        reg.close()
    with db(new_id) as conn:
        init_household_db(conn)
    return {"code": code, "token": create_app_token(new_id)}


@app.post("/api/login")
def login(payload: LoginRequest):
    conn = registry_db()
    try:
        row = conn.execute(
            "SELECT id FROM households WHERE code = ?", (payload.code.strip(),)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(403, "That code wasn't found")
    return {"token": create_app_token(row["id"])}


@protected.get("/api/state")
def get_state(unlock: Optional[str] = None, household_id: int = Depends(get_household_id)):
    unlocked_ids = set()
    if unlock:
        household_tokens = UNLOCK_TOKENS.get(household_id, {})
        for pair in unlock.split(","):
            if ":" not in pair:
                continue
            sid_str, token = pair.split(":", 1)
            try:
                sid = int(sid_str)
            except ValueError:
                continue
            if household_tokens.get(token) == sid:
                unlocked_ids.add(sid)

    with db(household_id) as conn:
        sections = [dict(r) for r in conn.execute(
            "SELECT * FROM sections ORDER BY sort_order, id"
        )]
        tasks = [dict(r) for r in conn.execute(
            "SELECT * FROM tasks ORDER BY done, sort_order"
        )]
        photos = [dict(r) for r in conn.execute(
            "SELECT * FROM photos ORDER BY created_at"
        )]
        albums = [dict(r) for r in conn.execute(
            "SELECT * FROM albums ORDER BY sort_order, id"
        )]
        activity = [dict(r) for r in conn.execute(
            "SELECT * FROM activity ORDER BY created_at DESC LIMIT 20"
        )]
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events ORDER BY date, slot"
        )]
        meal_plan = [dict(r) for r in conn.execute(
            "SELECT * FROM meal_plan ORDER BY weekday"
        )]
        appointments = [dict(r) for r in conn.execute(
            "SELECT * FROM appointments ORDER BY date, time"
        )]
    task_photos = {}
    album_photos = {}
    notes = []
    for p in photos:
        if p["task_id"] is not None:
            task_photos.setdefault(p["task_id"], []).append(p)
        elif p["album_id"] is not None:
            album_photos.setdefault(p["album_id"], []).append(p)
        else:
            notes.append(p)
    notes.sort(key=lambda p: p["created_at"], reverse=True)
    for aid in album_photos:
        album_photos[aid].sort(key=lambda p: p["sort_order"])
    for t in tasks:
        t["photos"] = task_photos.get(t["id"], [])
    for a in albums:
        a["photos"] = album_photos.get(a["id"], [])
    section_albums = {}
    for a in albums:
        section_albums.setdefault(a["section_id"], []).append(a)
    for s in sections:
        protected_flag = bool(s["password_hash"])
        del s["password_hash"]
        s["protected"] = protected_flag
        section_tasks = [t for t in tasks if t["section_id"] == s["id"]]
        section_album_list = section_albums.get(s["id"], [])
        if protected_flag and s["id"] not in unlocked_ids:
            s["locked"] = True
            s["locked_count"] = len(section_tasks)
            s["tasks"] = []
            s["albums"] = []
        else:
            s["locked"] = False
            s["tasks"] = section_tasks
            s["albums"] = section_album_list
    return {
        "sections": sections, "activity": activity, "notes": notes, "events": events,
        "meal_plan": meal_plan, "appointments": appointments,
    }


@protected.patch("/api/meal-plan/{weekday}")
def update_meal_plan(weekday: int, payload: MealPlanUpdate, household_id: int = Depends(get_household_id)):
    if weekday < 0 or weekday > 6:
        raise HTTPException(400, "weekday must be 0-6")
    with db(household_id) as conn:
        conn.execute(
            "UPDATE meal_plan SET text = ?, updated_by = ?, updated_at = ? WHERE weekday = ?",
            (payload.text.strip(), payload.updated_by, time.time(), weekday),
        )
        return {"ok": True}


@protected.post("/api/sections")
def create_section(payload: SectionCreate, household_id: int = Depends(get_household_id)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Section name can't be empty")
    with db(household_id) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM sections").fetchone()["c"]
        max_order = conn.execute("SELECT MAX(sort_order) m FROM sections").fetchone()["m"]
        color = SECTION_COLORS[n % len(SECTION_COLORS)]
        cur = conn.execute(
            "INSERT INTO sections (name, color, sort_order, created_at) VALUES (?,?,?,?)",
            (name, color, (max_order or 0) + 1, time.time()),
        )
        return {"id": cur.lastrowid, "name": name, "color": color}


@protected.patch("/api/sections/{section_id}")
def update_section(section_id: int, payload: SectionUpdate, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        section = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
        if not section:
            raise HTTPException(404, "Section not found")
        name = section["name"]
        color = section["color"]
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise HTTPException(400, "Section name can't be empty")
        if payload.color is not None:
            if payload.color not in SECTION_COLORS:
                raise HTTPException(400, "Not a valid color")
            color = payload.color
        conn.execute("UPDATE sections SET name = ?, color = ? WHERE id = ?", (name, color, section_id))
        return {"id": section_id, "name": name, "color": color}


@protected.post("/api/sections/{section_id}/move")
def move_section(section_id: int, payload: SectionMove, household_id: int = Depends(get_household_id)):
    if payload.direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")
    with db(household_id) as conn:
        sections = [dict(r) for r in conn.execute("SELECT id, sort_order FROM sections ORDER BY sort_order, id")]
        idx = next((i for i, s in enumerate(sections) if s["id"] == section_id), None)
        if idx is None:
            raise HTTPException(404, "Section not found")
        swap_idx = idx - 1 if payload.direction == "up" else idx + 1
        if swap_idx < 0 or swap_idx >= len(sections):
            return {"ok": True}  # already at the boundary, nothing to do
        a, b = sections[idx], sections[swap_idx]
        conn.execute("UPDATE sections SET sort_order = ? WHERE id = ?", (b["sort_order"], a["id"]))
        conn.execute("UPDATE sections SET sort_order = ? WHERE id = ?", (a["sort_order"], b["id"]))
        return {"ok": True}


@protected.put("/api/sections/{section_id}/protection")
def set_section_protection(section_id: int, payload: SectionProtection, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        section = conn.execute("SELECT password_hash FROM sections WHERE id = ?", (section_id,)).fetchone()
        if not section:
            raise HTTPException(404, "Section not found")
        currently_protected = bool(section["password_hash"])
        if currently_protected:
            if not payload.current_password or not verify_password(payload.current_password, section["password_hash"]):
                raise HTTPException(403, "Current password is wrong")
        new_password = (payload.new_password or "").strip()
        new_hash = hash_password(new_password) if new_password else None
        conn.execute("UPDATE sections SET password_hash = ? WHERE id = ?", (new_hash, section_id))
        return {"ok": True, "protected": bool(new_hash)}


@protected.post("/api/sections/{section_id}/unlock")
def unlock_section(section_id: int, payload: SectionUnlock, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        section = conn.execute("SELECT password_hash FROM sections WHERE id = ?", (section_id,)).fetchone()
        if not section:
            raise HTTPException(404, "Section not found")
        if not section["password_hash"] or not verify_password(payload.password, section["password_hash"]):
            raise HTTPException(403, "Wrong password")
        token = secrets.token_urlsafe(24)
        UNLOCK_TOKENS.setdefault(household_id, {})[token] = section_id
        return {"token": token}


@protected.delete("/api/sections/{section_id}")
def delete_section(section_id: int, unlock_token: Optional[str] = None, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        require_unlocked(conn, household_id, section_id, unlock_token)
        photo_rows = conn.execute(
            "SELECT filename, thumb_filename FROM photos WHERE task_id IN (SELECT id FROM tasks WHERE section_id = ?) "
            "OR album_id IN (SELECT id FROM albums WHERE section_id = ?)",
            (section_id, section_id),
        ).fetchall()
        conn.execute("DELETE FROM sections WHERE id = ?", (section_id,))
    delete_photo_files(
        [r["filename"] for r in photo_rows] + [r["thumb_filename"] for r in photo_rows],
        household_photos_dir(household_id),
    )
    return {"ok": True}


def get_album_or_404(conn, album_id: int):
    album = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not album:
        raise HTTPException(404, "Album not found")
    return album


def next_album_sort_order(conn, album_id: int) -> int:
    row = conn.execute("SELECT MAX(sort_order) m FROM photos WHERE album_id = ?", (album_id,)).fetchone()
    return (row["m"] + 1) if row["m"] is not None else 0


def next_section_task_sort_order(conn, section_id: int) -> int:
    row = conn.execute("SELECT MAX(sort_order) m FROM tasks WHERE section_id = ?", (section_id,)).fetchone()
    return (row["m"] + 1) if row["m"] is not None else 0


@protected.post("/api/albums")
def create_album(payload: AlbumCreate, household_id: int = Depends(get_household_id)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Album name can't be empty")
    with db(household_id) as conn:
        section = conn.execute("SELECT name FROM sections WHERE id = ?", (payload.section_id,)).fetchone()
        if not section:
            raise HTTPException(404, "Section not found")
        require_unlocked(conn, household_id, payload.section_id, payload.unlock_token)
        max_order = conn.execute(
            "SELECT MAX(sort_order) m FROM albums WHERE section_id = ?", (payload.section_id,)
        ).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO albums (section_id, name, sort_order, created_at) VALUES (?,?,?,?)",
            (payload.section_id, name, (max_order or 0) + 1, time.time()),
        )
        return {"id": cur.lastrowid, "name": name}


@protected.patch("/api/albums/{album_id}")
def update_album(album_id: int, payload: AlbumUpdate, household_id: int = Depends(get_household_id)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Album name can't be empty")
    with db(household_id) as conn:
        album = get_album_or_404(conn, album_id)
        require_unlocked(conn, household_id, album["section_id"], payload.unlock_token)
        conn.execute("UPDATE albums SET name = ? WHERE id = ?", (name, album_id))
        return {"id": album_id, "name": name}


@protected.post("/api/albums/{album_id}/move")
def move_album(album_id: int, payload: AlbumMove, household_id: int = Depends(get_household_id)):
    if payload.direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")
    with db(household_id) as conn:
        album = get_album_or_404(conn, album_id)
        require_unlocked(conn, household_id, album["section_id"], payload.unlock_token)
        siblings = [dict(r) for r in conn.execute(
            "SELECT id, sort_order FROM albums WHERE section_id = ? ORDER BY sort_order, id",
            (album["section_id"],),
        )]
        idx = next((i for i, a in enumerate(siblings) if a["id"] == album_id), None)
        swap_idx = idx - 1 if payload.direction == "up" else idx + 1
        if swap_idx < 0 or swap_idx >= len(siblings):
            return {"ok": True}  # already at the boundary, nothing to do
        a, b = siblings[idx], siblings[swap_idx]
        conn.execute("UPDATE albums SET sort_order = ? WHERE id = ?", (b["sort_order"], a["id"]))
        conn.execute("UPDATE albums SET sort_order = ? WHERE id = ?", (a["sort_order"], b["id"]))
        return {"ok": True}


@protected.post("/api/albums/{album_id}/reorder")
def reorder_album_photos(album_id: int, payload: AlbumReorder, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        album = get_album_or_404(conn, album_id)
        if album["section_id"] is not None:
            require_unlocked(conn, household_id, album["section_id"], payload.unlock_token)
        existing_ids = {r["id"] for r in conn.execute("SELECT id FROM photos WHERE album_id = ?", (album_id,))}
        if set(payload.photo_ids) != existing_ids:
            raise HTTPException(400, "photo_ids must match exactly the photos currently in this album")
        for i, pid in enumerate(payload.photo_ids):
            conn.execute("UPDATE photos SET sort_order = ? WHERE id = ?", (i, pid))
        return {"ok": True}


@protected.delete("/api/albums/{album_id}")
def delete_album(album_id: int, unlock_token: Optional[str] = None, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        album = get_album_or_404(conn, album_id)
        require_unlocked(conn, household_id, album["section_id"], unlock_token)
        photo_rows = conn.execute("SELECT filename, thumb_filename FROM photos WHERE album_id = ?", (album_id,)).fetchall()
        conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    delete_photo_files(
        [r["filename"] for r in photo_rows] + [r["thumb_filename"] for r in photo_rows],
        household_photos_dir(household_id),
    )
    return {"ok": True}


@protected.post("/api/tasks")
def create_task(payload: TaskCreate, household_id: int = Depends(get_household_id)):
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Task text can't be empty")
    with db(household_id) as conn:
        section = conn.execute(
            "SELECT name FROM sections WHERE id = ?", (payload.section_id,)
        ).fetchone()
        if not section:
            raise HTTPException(404, "Section not found")
        require_unlocked(conn, household_id, payload.section_id, payload.unlock_token)
        cur = conn.execute(
            "INSERT INTO tasks (section_id, text, added_by, sort_order, created_at) VALUES (?,?,?,?,?)",
            (payload.section_id, text, payload.added_by, next_section_task_sort_order(conn, payload.section_id), time.time()),
        )
        log_activity(
            conn,
            f"{payload.added_by} added “{text}” to {section['name']}",
        )
        return {"id": cur.lastrowid}


@protected.post("/api/tasks/bulk")
def create_tasks_bulk(payload: TaskBulkCreate, household_id: int = Depends(get_household_id)):
    texts = [t.strip() for t in payload.texts if t.strip()]
    if not texts:
        raise HTTPException(400, "No items to add")
    with db(household_id) as conn:
        section = conn.execute(
            "SELECT name FROM sections WHERE id = ?", (payload.section_id,)
        ).fetchone()
        if not section:
            raise HTTPException(404, "Section not found")
        require_unlocked(conn, household_id, payload.section_id, payload.unlock_token)
        now = time.time()
        next_order = next_section_task_sort_order(conn, payload.section_id)
        ids = []
        for i, text in enumerate(texts):
            cur = conn.execute(
                "INSERT INTO tasks (section_id, text, added_by, sort_order, created_at) VALUES (?,?,?,?,?)",
                (payload.section_id, text, payload.added_by, next_order + i, now + i * 0.0001),
            )
            ids.append(cur.lastrowid)

        if len(texts) == 1:
            msg = f"{payload.added_by} added “{texts[0]}” to {section['name']}"
        else:
            preview = texts[:5]
            preview_str = ", ".join(f"“{t}”" for t in preview)
            extra = len(texts) - len(preview)
            if extra > 0:
                preview_str += f" +{extra} more"
            msg = f"{payload.added_by} added {len(texts)} items to {section['name']}: {preview_str}"
        log_activity(conn, msg)
        return {"ids": ids}


@protected.post("/api/sections/{section_id}/reorder-tasks")
def reorder_tasks(section_id: int, payload: TaskReorder, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        section = conn.execute("SELECT id FROM sections WHERE id = ?", (section_id,)).fetchone()
        if not section:
            raise HTTPException(404, "Section not found")
        require_unlocked(conn, household_id, section_id, payload.unlock_token)
        existing_ids = {r["id"] for r in conn.execute("SELECT id FROM tasks WHERE section_id = ?", (section_id,))}
        if set(payload.task_ids) != existing_ids:
            raise HTTPException(400, "task_ids must match exactly the tasks currently in this list")
        for i, tid in enumerate(payload.task_ids):
            conn.execute("UPDATE tasks SET sort_order = ? WHERE id = ?", (i, tid))
        return {"ok": True}


@protected.patch("/api/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        require_unlocked(conn, household_id, task["section_id"], payload.unlock_token)
        conn.execute(
            "UPDATE tasks SET done = ?, done_by = ?, done_at = ? WHERE id = ?",
            (1 if payload.done else 0, payload.by if payload.done else None,
             time.time() if payload.done else None, task_id),
        )
        if payload.done:
            log_activity(conn, f"{payload.by} ticked off “{task['text']}”")
    return {"ok": True}


@protected.patch("/api/tasks/{task_id}/text")
def update_task_text(task_id: int, payload: TaskTextUpdate, household_id: int = Depends(get_household_id)):
    new_text = payload.text.strip()
    if not new_text:
        raise HTTPException(400, "Task text can't be empty")
    with db(household_id) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        require_unlocked(conn, household_id, task["section_id"], payload.unlock_token)
        old_text = task["text"]
        conn.execute("UPDATE tasks SET text = ? WHERE id = ?", (new_text, task_id))
        if old_text != new_text:
            log_activity(conn, f"{payload.by} changed “{old_text}” to “{new_text}”")
    return {"ok": True}


@protected.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, unlock_token: Optional[str] = None, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        task = conn.execute("SELECT section_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        require_unlocked(conn, household_id, task["section_id"], unlock_token)
        photo_rows = conn.execute("SELECT filename, thumb_filename FROM photos WHERE task_id = ?", (task_id,)).fetchall()
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    delete_photo_files(
        [r["filename"] for r in photo_rows] + [r["thumb_filename"] for r in photo_rows],
        household_photos_dir(household_id),
    )
    return {"ok": True}


@protected.post("/api/photos")
async def upload_photo(
    file: Optional[UploadFile] = File(None),
    added_by: str = Form(...),
    task_id: Optional[int] = Form(None),
    caption: Optional[str] = Form(None),
    unlock_token: Optional[str] = Form(None),
    household_id: int = Depends(get_household_id),
):
    cap = (caption or "").strip() or None
    filename = None
    media_type = "photo"
    thumb_filename = None
    photos_dir = household_photos_dir(household_id)

    if file is not None and file.filename:
        content_type = file.content_type or ""
        if not content_type.startswith(("image/", "video/", "audio/")):
            raise HTTPException(400, "Only image, video, or audio files are allowed")
        raw = await file.read()
        max_bytes = MAX_VIDEO_BYTES if content_type.startswith("video/") else MAX_UPLOAD_BYTES
        if len(raw) > max_bytes:
            raise HTTPException(400, f"File is too large (max {max_bytes // (1024 * 1024)}MB)")
        filename, media_type, thumb_filename = save_media_file(raw, content_type, photos_dir)
    elif not cap:
        raise HTTPException(400, "Add a photo or write a note")

    with db(household_id) as conn:
        task = None
        if task_id is not None:
            task = conn.execute("SELECT text, section_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                delete_photo_files([filename, thumb_filename], photos_dir)
                raise HTTPException(404, "Task not found")
            require_unlocked(conn, household_id, task["section_id"], unlock_token)
        cur = conn.execute(
            "INSERT INTO photos (task_id, caption, filename, media_type, thumb_filename, added_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, cap, filename, media_type, thumb_filename, added_by, time.time()),
        )
        if task is not None:
            msg = (f"{added_by} added a photo to “{task['text']}”" if filename
                   else f"{added_by} added a note to “{task['text']}”: “{cap}”")
            log_activity(conn, msg)
        elif filename:
            log_activity(conn, f"{added_by} pinned a note" + (f": “{cap}”" if cap else ""))
        else:
            log_activity(conn, f"{added_by} wrote a note: “{cap}”")
        return {
            "id": cur.lastrowid, "task_id": task_id, "caption": cap,
            "filename": filename, "media_type": media_type, "thumb_filename": thumb_filename, "added_by": added_by,
        }


@protected.post("/api/photos/bulk")
async def upload_photos_bulk(
    files: list[UploadFile] = File(...),
    added_by: str = Form(...),
    album_id: Optional[int] = Form(None),
    task_id: Optional[int] = Form(None),
    unlock_token: Optional[str] = Form(None),
    household_id: int = Depends(get_household_id),
):
    if not files:
        raise HTTPException(400, "No photos selected")
    photos_dir = household_photos_dir(household_id)

    with db(household_id) as conn:
        album_name = None
        task = None
        if album_id is not None:
            album = get_album_or_404(conn, album_id)
            require_unlocked(conn, household_id, album["section_id"], unlock_token)
            album_name = album["name"]
        elif task_id is not None:
            task = conn.execute("SELECT text, section_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(404, "Task not found")
            require_unlocked(conn, household_id, task["section_id"], unlock_token)

        next_order = next_album_sort_order(conn, album_id) if album_id is not None else 0

        ids = []
        for file in files:
            content_type = file.content_type or ""
            if not content_type.startswith(("image/", "video/")):
                continue
            raw = await file.read()
            max_bytes = MAX_VIDEO_BYTES if content_type.startswith("video/") else MAX_UPLOAD_BYTES
            if len(raw) > max_bytes:
                continue
            filename, media_type, thumb_filename = save_media_file(raw, content_type, photos_dir)
            cur = conn.execute(
                "INSERT INTO photos (task_id, album_id, caption, filename, media_type, thumb_filename, sort_order, added_by, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, album_id, None, filename, media_type, thumb_filename, next_order, added_by, time.time()),
            )
            next_order += 1
            ids.append(cur.lastrowid)

        if not ids:
            raise HTTPException(400, "No valid images uploaded")

        count = len(ids)
        plural = "s" if count != 1 else ""
        if task is not None:
            msg = f"{added_by} added {count} photo{plural} to “{task['text']}”"
        elif album_name is not None:
            msg = f"{added_by} added {count} photo{plural} to the {album_name} album"
        else:
            msg = f"{added_by} added {count} photo{plural}"
        log_activity(conn, msg)
        return {"ids": ids}


@protected.patch("/api/photos/{photo_id}")
def update_photo(photo_id: int, payload: PhotoUpdate, household_id: int = Depends(get_household_id)):
    caption = payload.caption.strip()
    with db(household_id) as conn:
        row = conn.execute(
            "SELECT p.filename, a.section_id AS album_section_id, t.section_id AS task_section_id "
            "FROM photos p "
            "LEFT JOIN tasks t ON t.id = p.task_id "
            "LEFT JOIN albums a ON a.id = p.album_id "
            "WHERE p.id = ?",
            (photo_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Photo not found")
        effective_section_id = row["task_section_id"] if row["task_section_id"] is not None else row["album_section_id"]
        if effective_section_id is not None:
            require_unlocked(conn, household_id, effective_section_id, payload.unlock_token)
        if not row["filename"] and not caption:
            raise HTTPException(400, "Note can't be empty")
        conn.execute("UPDATE photos SET caption = ? WHERE id = ?", (caption or None, photo_id))
        return {"ok": True}


@protected.post("/api/photos/{photo_id}/move")
def move_photo(photo_id: int, payload: PhotoMove, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        row = conn.execute(
            "SELECT p.album_id, a.section_id AS source_section_id "
            "FROM photos p LEFT JOIN albums a ON a.id = p.album_id WHERE p.id = ?",
            (photo_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Photo not found")
        if row["album_id"] is None:
            raise HTTPException(400, "This isn't an album photo")
        dest_album = get_album_or_404(conn, payload.album_id)
        if row["source_section_id"] is not None:
            require_unlocked(conn, household_id, row["source_section_id"], payload.unlock_token)
        if dest_album["section_id"] is not None:
            require_unlocked(conn, household_id, dest_album["section_id"], payload.dest_unlock_token)
        new_order = next_album_sort_order(conn, payload.album_id)
        conn.execute(
            "UPDATE photos SET album_id = ?, sort_order = ? WHERE id = ?",
            (payload.album_id, new_order, photo_id),
        )
        return {"ok": True}


@protected.post("/api/photos/{photo_id}/copy")
def copy_photo(photo_id: int, payload: PhotoMove, household_id: int = Depends(get_household_id)):
    photos_dir = household_photos_dir(household_id)
    with db(household_id) as conn:
        row = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Photo not found")
        if row["album_id"] is None:
            raise HTTPException(400, "This isn't an album photo")
        source_album = get_album_or_404(conn, row["album_id"])
        if source_album["section_id"] is not None:
            require_unlocked(conn, household_id, source_album["section_id"], payload.unlock_token)
        dest_album = get_album_or_404(conn, payload.album_id)
        if dest_album["section_id"] is not None:
            require_unlocked(conn, household_id, dest_album["section_id"], payload.dest_unlock_token)

        # duplicate the file on disk too — if two rows shared one filename,
        # deleting either copy would delete the file out from under the other
        new_filename = None
        if row["filename"]:
            ext = os.path.splitext(row["filename"])[1]
            new_filename = f"{uuid.uuid4().hex}{ext}"
            shutil.copyfile(
                os.path.join(photos_dir, row["filename"]),
                os.path.join(photos_dir, new_filename),
            )
        new_thumb_filename = None
        if row["thumb_filename"]:
            new_thumb_filename = f"{uuid.uuid4().hex}_thumb.jpg"
            shutil.copyfile(
                os.path.join(photos_dir, row["thumb_filename"]),
                os.path.join(photos_dir, new_thumb_filename),
            )

        new_order = next_album_sort_order(conn, payload.album_id)
        cur = conn.execute(
            "INSERT INTO photos (task_id, album_id, caption, filename, media_type, thumb_filename, sort_order, added_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (None, payload.album_id, row["caption"], new_filename, row["media_type"], new_thumb_filename, new_order, row["added_by"], time.time()),
        )
        return {"id": cur.lastrowid}


@protected.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: int, unlock_token: Optional[str] = None, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        row = conn.execute(
            "SELECT p.filename, p.thumb_filename, a.section_id AS album_section_id, t.section_id AS task_section_id "
            "FROM photos p "
            "LEFT JOIN tasks t ON t.id = p.task_id "
            "LEFT JOIN albums a ON a.id = p.album_id "
            "WHERE p.id = ?",
            (photo_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Photo not found")
        effective_section_id = row["task_section_id"] if row["task_section_id"] is not None else row["album_section_id"]
        if effective_section_id is not None:
            require_unlocked(conn, household_id, effective_section_id, unlock_token)
        conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    delete_photo_files([row["filename"], row["thumb_filename"]], household_photos_dir(household_id))
    return {"ok": True}


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


@protected.post("/api/events")
def create_event(payload: EventCreate, household_id: int = Depends(get_household_id)):
    if payload.slot not in ("day", "evening"):
        raise HTTPException(400, "slot must be 'day' or 'evening'")
    if not DATE_RE.match(payload.date):
        raise HTTPException(400, "Invalid date format")
    title = payload.title.strip()
    if not title:
        raise HTTPException(400, "Event title can't be empty")
    with db(household_id) as conn:
        cur = conn.execute(
            "INSERT INTO events (date, slot, title, added_by, created_at) VALUES (?,?,?,?,?)",
            (payload.date, payload.slot, title, payload.added_by, time.time()),
        )
        log_activity(
            conn,
            f"{payload.added_by} added “{title}” to the calendar ({payload.date}, {payload.slot})",
        )
        return {"id": cur.lastrowid}


@protected.delete("/api/events/{event_id}")
def delete_event(event_id: int, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        row = conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Event not found")
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return {"ok": True}


@protected.post("/api/appointments")
def create_appointment(payload: AppointmentCreate, household_id: int = Depends(get_household_id)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Appointment name can't be empty")
    if not DATE_RE.match(payload.date):
        raise HTTPException(400, "Invalid date format")
    if not TIME_RE.match(payload.time):
        raise HTTPException(400, "Invalid time format")
    if payload.lead_minutes not in APPOINTMENT_LEAD_OPTIONS:
        raise HTTPException(400, "Not a valid reminder option")
    with db(household_id) as conn:
        cur = conn.execute(
            "INSERT INTO appointments (name, date, time, lead_minutes, added_by, reminded, created_at) "
            "VALUES (?,?,?,?,?,0,?)",
            (name, payload.date, payload.time, payload.lead_minutes, payload.added_by, time.time()),
        )
        log_activity(
            conn,
            f"{payload.added_by} added an appointment: “{name}” ({payload.date} {payload.time})",
        )
        return {"id": cur.lastrowid}


@protected.patch("/api/appointments/{appointment_id}")
def update_appointment(appointment_id: int, payload: AppointmentUpdate, household_id: int = Depends(get_household_id)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Appointment name can't be empty")
    if not DATE_RE.match(payload.date):
        raise HTTPException(400, "Invalid date format")
    if not TIME_RE.match(payload.time):
        raise HTTPException(400, "Invalid time format")
    if payload.lead_minutes not in APPOINTMENT_LEAD_OPTIONS:
        raise HTTPException(400, "Not a valid reminder option")
    with db(household_id) as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Appointment not found")
        # only re-arm the reminder if the schedule actually moved — editing just
        # the name after a reminder already fired shouldn't trigger a second push
        schedule_changed = (
            row["date"] != payload.date
            or row["time"] != payload.time
            or row["lead_minutes"] != payload.lead_minutes
        )
        new_reminded = 0 if schedule_changed else row["reminded"]
        conn.execute(
            "UPDATE appointments SET name = ?, date = ?, time = ?, lead_minutes = ?, reminded = ? WHERE id = ?",
            (name, payload.date, payload.time, payload.lead_minutes, new_reminded, appointment_id),
        )
        return {"ok": True}


@protected.delete("/api/appointments/{appointment_id}")
def delete_appointment(appointment_id: int, household_id: int = Depends(get_household_id)):
    with db(household_id) as conn:
        row = conn.execute("SELECT id FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Appointment not found")
        conn.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
    return {"ok": True}


def check_appointment_reminders():
    # notifications are in-app only in the multi-tenant version (the activity
    # feed each household already has) — a single global NTFY_TOPIC doesn't
    # make sense once there can be many households, and there's no per-household
    # notification setting yet (see plan notes), so push notifications are
    # deliberately not wired up here to avoid one household's reminders being
    # pushed to a topic another household might also be subscribed to.
    now = time.time()
    for hid in all_household_ids():
        with db(hid) as conn:
            rows = conn.execute("SELECT * FROM appointments WHERE reminded = 0").fetchall()
            for r in rows:
                try:
                    appt_epoch = datetime.strptime(
                        f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=UK_TZ).timestamp()
                except ValueError:
                    conn.execute("UPDATE appointments SET reminded = 1 WHERE id = ?", (r["id"],))
                    continue
                remind_at = appt_epoch - r["lead_minutes"] * 60
                if now < remind_at:
                    continue
                conn.execute("UPDATE appointments SET reminded = 1 WHERE id = ?", (r["id"],))


async def appointment_reminder_loop():
    while True:
        try:
            check_appointment_reminders()
        except Exception:
            pass  # a bad iteration should never kill the background loop
        await asyncio.sleep(60)


@app.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(appointment_reminder_loop())


@app.get("/uploads/{filename}")
def get_upload(filename: str, household_id: int = Depends(get_household_id)):
    # not a static mount — uploaded photos are real user content, so they go
    # through the same login gate as everything else in /api/*.
    # no-store: a shared CDN cache in front of this must never cache a
    # response here, or it could later serve a photo to a request that
    # never actually passed the auth check.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(404)
    path = os.path.join(household_photos_dir(household_id), filename)
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})


app.include_router(protected)
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
