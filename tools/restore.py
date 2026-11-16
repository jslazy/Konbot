"""Restore / verify a Leafy backup zip.

    python tools/restore.py leafy_backup.zip [target_folder]      (default target: the bot folder = parent of tools/)
    python tools/restore.py leafy_backup.zip --check              (only verify, write nothing)

- verifies every file against BACKUP_MANIFEST.json (sha1) and runs SQLite integrity_check on data/leafy.db
- extracts everything (code, assets, data/ incl. banner, export/)
- the backup contains DISCORD_TOKEN / BACKUP_WEBHOOK (unless BACKUP_REDACT=1 was set); if they are REMOVED, the ones
  already in the target's .env are kept
"""
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

SECRET = re.compile(r"TOKEN|SECRET|PASSWORD|WEBHOOK", re.I)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check_only = "--check" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    src = Path(args[0])
    target = Path(args[1]) if len(args) > 1 else Path(__file__).resolve().parent.parent
    z = zipfile.ZipFile(src)
    bad = z.testzip()
    if bad:
        raise SystemExit(f"corrupt zip entry: {bad}")
    manifest = json.loads(z.read("BACKUP_MANIFEST.json"))
    problems = []
    for f in manifest["files"]:
        if f["path"] == "BACKUP_MANIFEST.json":
            continue
        try:
            data = z.read(f["path"])
        except KeyError:
            problems.append(f"missing: {f['path']}")
            continue
        if hashlib.sha1(data).hexdigest() != f["sha1"]:
            problems.append(f"changed: {f['path']}")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "leafy.db"
        db.write_bytes(z.read("data/leafy.db"))
        c = sqlite3.connect(db)
        res = c.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("users", "shop_items", "purchases", "settings", "point_log")}
        c.close()
    print(f"backup #{manifest.get('backup_number')} of {manifest.get('bot')} · {manifest.get('created')}")
    print(f"files: {len(manifest['files'])} · database integrity: {res} · rows: {counts}")
    if problems:
        print("PROBLEMS:\n  " + "\n  ".join(problems))
        raise SystemExit(1)
    print("all files match the manifest")
    if check_only:
        return
    old_env = {}
    env = target / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                old_env[k.strip().removeprefix("export ").strip()] = line
    target.mkdir(parents=True, exist_ok=True)
    for name in z.namelist():
        if name.endswith("/") or name == "leafy_backup.zip":
            continue
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if name == ".env":
            lines = []
            for line in z.read(name).decode("utf-8", "replace").splitlines():
                k = line.partition("=")[0].strip().removeprefix("export ").strip()
                value = line.partition("=")[2].strip().strip("\"'")
                keep_old = SECRET.search(k) and k in old_env and value in ("", "REMOVED")   # backup has the real value -> use it
                lines.append(old_env[k] if keep_old else line)
            dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            with z.open(name) as s, open(dest, "wb") as d:
                shutil.copyfileobj(s, d)
    for ext in ("-wal", "-shm"):                      # stale journal of an older db would corrupt the restored one
        (target / "data" / f"leafy.db{ext}").unlink(missing_ok=True)
    print(f"restored into {target}")
    still = [k for k in re.findall(r"^\s*(\w*(?:TOKEN|WEBHOOK)\w*)\s*=\s*REMOVED", env.read_text(errors="replace") if env.exists() else "", re.M)]
    if still:
        print("now put these back into .env:", ", ".join(still))
    print("then: pip install -r requirements.txt && python main.py")


if __name__ == "__main__":
    main()
