"""Restore / verify Leafy backup zips (offline, no bot needed).

    python tools/restore.py leafy_code.zip leafy_data.zip [target_folder]
    python tools/restore.py leafy_data.zip --check              (only verify, write nothing)

The webhook backup sends TWO zips: leafy_code.zip (code, assets, .env) and leafy_data.zip (data/ folder).
Give one or both (old single-zip backups work too). Target default = the bot folder (parent of tools/).
Every file is checked against the manifest (sha1) and data/leafy.db gets a SQLite integrity check.
If the code zip has REMOVED secrets (BACKUP_REDACT=1), the ones already in the target's .env are kept.

Running bot? Don't use this: send the data zip to the bot with `$backupload` and it merges by itself.
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
MANIFESTS = ("BACKUP_MANIFEST.json", "DATA_MANIFEST.json")


def verify(z: zipfile.ZipFile, label: str) -> list:
    bad = z.testzip()
    if bad:
        raise SystemExit(f"{label}: corrupt entry {bad}")
    name = next((m for m in MANIFESTS if m in z.namelist()), None)
    if name is None:
        raise SystemExit(f"{label}: no manifest inside, is this a Leafy backup?")
    manifest = json.loads(z.read(name))
    problems = []
    for f in manifest["files"]:
        try:
            data = z.read(f["path"])
        except KeyError:
            problems.append(f"missing: {f['path']}")
            continue
        if hashlib.sha1(data).hexdigest() != f["sha1"]:
            problems.append(f"changed: {f['path']}")
    if "data/leafy.db" in z.namelist():
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "leafy.db"
            db.write_bytes(z.read("data/leafy.db"))
            c = sqlite3.connect(db)
            res = c.execute("PRAGMA integrity_check").fetchone()[0]
            rows = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("users", "shop_items", "purchases", "settings", "point_log")}
            c.close()
        print(f"{label}: database {res} · rows {rows}")
    print(f"{label}: backup #{manifest.get('backup_number')} of {manifest.get('bot')} · {manifest.get('created')} "
          f"· {len(manifest['files'])} files")
    return problems


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check_only = "--check" in sys.argv
    zips = [Path(a) for a in args if a.lower().endswith(".zip")]
    rest = [a for a in args if not a.lower().endswith(".zip")]
    if not zips:
        raise SystemExit(__doc__)
    target = Path(rest[0]) if rest else Path(__file__).resolve().parent.parent

    opened = [(p, zipfile.ZipFile(p)) for p in zips]
    problems = []
    for p, z in opened:
        problems += [f"{p.name}: {x}" for x in verify(z, p.name)]
    if problems:
        print("PROBLEMS:\n  " + "\n  ".join(problems))
        raise SystemExit(1)
    print("all files match their manifests")
    if check_only:
        return

    env = target / ".env"
    old_env = {}
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                old_env[k.strip().removeprefix("export ").strip()] = line
    target.mkdir(parents=True, exist_ok=True)
    for p, z in opened:
        for name in z.namelist():
            if name.endswith("/") or name.endswith(".zip") or ".." in Path(name).parts:
                continue
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if name == ".env":
                lines = []
                for line in z.read(name).decode("utf-8", "replace").splitlines():
                    k = line.partition("=")[0].strip().removeprefix("export ").strip()
                    value = line.partition("=")[2].strip().strip("\"'")
                    keep_old = SECRET.search(k) and k in old_env and value in ("", "REMOVED")
                    lines.append(old_env[k] if keep_old else line)
                dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
            else:
                with z.open(name) as s, open(dest, "wb") as d:
                    shutil.copyfileobj(s, d)
    for ext in ("-wal", "-shm"):                      # a stale journal of an older db would corrupt the restored one
        (target / "data" / f"leafy.db{ext}").unlink(missing_ok=True)
    print(f"restored into {target}")
    if env.exists():
        still = re.findall(r"^\s*(\w*(?:TOKEN|WEBHOOK)\w*)\s*=\s*REMOVED", env.read_text(errors="replace"), re.M)
        if still:
            print("put these back into .env:", ", ".join(still))
    print("then: pip install -r requirements.txt && python main.py")


if __name__ == "__main__":
    main()
