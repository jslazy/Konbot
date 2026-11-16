import os
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env", override=True)       # .env always wins (an empty host/panel variable used to hide it)
except ImportError:
    pass

TOKEN = os.getenv("DISCORD_TOKEN", "")
PREFIX = os.getenv("PREFIX", "$")
TIMEZONE = ZoneInfo("Asia/Kolkata")

AUTO_SYNC = os.getenv("AUTO_SYNC", "1") != "0"      # slash commands auto-sync on start (only if changed)
VC_PREFIX = ""                                     # custom VC name = just the username (no prefix)

GREEN = 0x4ADE80
BLURPLE = 0x5865F2
MENTIONS_PER_PAGE = 5

DATA_DIR = BASE / "data"                           # every data file lives here (and is part of the backup)
DATA_DIR.mkdir(exist_ok=True)
for _name in ("afk_data.json", "vc_data.json", "leafy.db", "leafy.db-wal", "leafy.db-shm"):   # move files of older versions
    _old = BASE / _name
    if _old.exists() and not (DATA_DIR / _name).exists():
        shutil.move(str(_old), str(DATA_DIR / _name))

AFK_FILE = DATA_DIR / "afk_data.json"
VC_FILE = DATA_DIR / "vc_data.json"
ICON_DIR = BASE / "assets" / "icons"
PANEL_IMAGE = BASE / "assets" / "panel.png"

DB_FILE = DATA_DIR / "leafy.db"                    # points, profiles, shop, purchases, settings
BACKUP_WEBHOOK = os.getenv("BACKUP_WEBHOOK", "").strip().strip("\"'").strip()
BACKUP_REDACT = os.getenv("BACKUP_REDACT", "0").strip().lower() in ("1", "true", "yes", "on")   # 1 = remove token/webhook from the backup zip (default: keep them)
BACKUP_MODE = os.getenv("BACKUP_MODE", "new").strip().lower()    # new = new message each time + delete the previous one, keep = keep every message
