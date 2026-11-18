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

# extra bot-owner user IDs (comma separated, optional). The Discord application owner is always the owner too.
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", os.getenv("OWNER_ID", "")).replace(" ", "").split(",") if x.isdigit()}

BRAND = 0xFFB832          # Kon golden yellow (palette: FFD65A / FFB832 / FF9B22 / F58219 / D95A12)
BLURPLE = 0x5865F2
MENTIONS_PER_PAGE = 5

DATA_DIR = BASE / "data"                           # every data file lives here (and is part of the backup)
DATA_DIR.mkdir(exist_ok=True)
for _name in ("afk_data.json", "vc_data.json", "leafy.db", "leafy.db-wal", "leafy.db-shm"):   # move files of older versions
    _old = BASE / _name
    if _old.exists() and not (DATA_DIR / _name).exists():
        shutil.move(str(_old), str(DATA_DIR / _name))
for _ext in ("", "-wal", "-shm"):                  # rebrand: the old Leafy database becomes kon.db (once, nothing is lost)
    _legacy, _new = DATA_DIR / f"leafy.db{_ext}", DATA_DIR / f"kon.db{_ext}"
    if _legacy.exists() and not _new.exists():
        shutil.move(str(_legacy), str(_new))

AFK_FILE = DATA_DIR / "afk_data.json"
VC_FILE = DATA_DIR / "vc_data.json"
ICON_DIR = BASE / "assets" / "icons"
PANEL_IMAGE = BASE / "assets" / "panel.png"

DB_FILE = DATA_DIR / "kon.db"                    # points, profiles, shop, purchases, settings
ASSET_CHANNEL_ID = int(os.getenv("ASSET_CHANNEL_ID", "0") or 0)    # private channel that hosts uploaded banners/GIFs (only their LINK is saved in data/)
BACKUP_WEBHOOK = os.getenv("BACKUP_WEBHOOK", "").strip().strip("\"'").strip()
BACKUP_REDACT = os.getenv("BACKUP_REDACT", "0").strip().lower() in ("1", "true", "yes", "on")   # 1 = remove token/webhook from the backup zip (default: keep them)
BACKUP_MODE = os.getenv("BACKUP_MODE", "new").strip().lower()    # new = new message each time + delete the previous one, keep = keep every message
