"""Webhook backup.

BACKUP_WEBHOOK (in .env) gets a detailed message with the FULL zip of the whole bot (code, assets, data/ folder,
.env INCLUDING the token and the webhook - set BACKUP_REDACT=1 in .env if you want them removed). It is sent automatically:
  - once when the bot starts,
  - every 30 seconds after that (no command needed),
  - once more when the bot stops (status = offline).
Every backup is a NEW message; the previous backup message is deleted so the channel does not fill up.
BACKUP_MODE=keep in .env keeps every message instead of deleting the previous one.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import platform
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

from core import checks, export
from core.config import AFK_FILE, BACKUP_MODE, BACKUP_REDACT, BACKUP_WEBHOOK, BASE, BLURPLE, GREEN
from core.errors import VCError
from core.icons import I, ok

log = logging.getLogger("leafy.backup")

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".idea", ".vscode",
             ".local", ".cache", ".pip", ".config", "site-packages", "pip-cache"}   # host/pip folders are not part of the bot
SKIP_FILES = {"leafy.db", "leafy.db-wal", "leafy.db-shm"}       # the live db is added as a consistent snapshot
SECRET_LINE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|WEBHOOK)[A-Za-z0-9_]*\s*=).*$",
                         re.I | re.M)
ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=", re.M)
WEBHOOK_LIMIT = 10 * 1024 * 1024
INTERVAL = 30       # seconds - periodic refresh
REASONS = {"start": "Bot started", "interval": "Automatic (every 30s)", "change": "Change detected",
           "manual": "Manual refresh", "stop": "Bot stopped"}


def redact_env(raw: bytes) -> bytes:
    return SECRET_LINE.sub(r"\1REMOVED", raw.decode("utf-8", "replace")).encode()


def iter_files(base: Path):
    """(relative posix name, Path) of every file that belongs in the backup."""
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            if f in SKIP_FILES or f.endswith((".pyc", ".tmp", ".zip")) or f == "BACKUP_MANIFEST.json":
                continue                # .zip = older backups lying around (would nest / bloat); the manifest is rebuilt each time
            p = Path(root) / f
            yield p.relative_to(base).as_posix(), p


def build_zip(base: Path, db_snapshot: Optional[str], meta: dict, cfg_export: Optional[dict] = None):
    """Runs in a worker thread -> dict(zip, digest, files{name:(size,sha1)}, raw_total).
    cfg_export = result of core.export.collect() (readable server config) - the player / database exports are made here."""
    digest = hashlib.sha256()
    buf = io.BytesIO()
    files: dict[str, tuple[int, str]] = {}
    env_keys, env_removed = [], []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        def add(name: str, data: bytes):
            h = hashlib.sha1(data).hexdigest()
            files[name] = (len(data), h)
            digest.update(name.encode() + b"\0" + h.encode())
            z.writestr(name, data)

        for name, p in iter_files(base):
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if p.name == ".env":
                text = data.decode("utf-8", "replace")
                env_keys = ENV_KEY.findall(text)
                if BACKUP_REDACT:                       # default: token + webhook stay in the backup
                    env_removed = [k for k in env_keys if re.search("TOKEN|SECRET|PASSWORD|WEBHOOK", k, re.I)]
                    data = redact_env(data)
            add(name, data)
        if db_snapshot:
            add("data/leafy.db", Path(db_snapshot).read_bytes())
        if cfg_export:
            add("export/SERVER_CONFIG.md", cfg_export["md"])
            add("export/SERVER_CONFIG.json", cfg_export["json"])
            add("export/afk.json", cfg_export["afk"])
            add("export/custom_vc_channels.json", cfg_export["vc_json"])
            for gid, files_ in cfg_export.get("per_server", {}).items():
                add(f"export/servers/{gid}/config.md", files_["md"])
                add(f"export/servers/{gid}/config.json", files_["json"])
                add(f"export/servers/{gid}/data.json", files_["data"])
            if db_snapshot:
                for name, data in export.db_extras(db_snapshot, cfg_export["names"]).items():
                    add(name, data)
        manifest = {
            **meta, "files": [{"path": n, "size": s, "sha1": h} for n, (s, h) in sorted(files.items())],
            "restore": ["unzip", "put your DISCORD_TOKEN (and BACKUP_WEBHOOK) back into .env",
                        "pip install -r requirements.txt", "python main.py"],
        }
        z.writestr("BACKUP_MANIFEST.json", json.dumps(manifest, indent=2))
    return {"zip": buf.getvalue(), "digest": digest.hexdigest(), "files": files, "env_keys": env_keys,
            "env_removed": env_removed, "raw_total": sum(s for s, _ in files.values())}


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._fp = None
        self._last_push = 0.0
        self._last_digest: Optional[str] = None
        self._prev_files: dict[str, tuple[int, str]] = {}
        self._first = True
        self.started = time.time()
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None

    @property
    def cfg(self) -> dict:
        return self.store.data.setdefault("backup", {})

    async def cog_load(self):
        log.info("backup webhook: %s", "configured" if BACKUP_WEBHOOK else "NOT SET (check .env: BACKUP_WEBHOOK=...)")
        self.session = aiohttp.ClientSession()
        self.watcher.start()

    async def cog_unload(self):
        self.watcher.cancel()
        if self.session:
            await self.session.close()

    # ---------- triggers ----------
    def fingerprint(self):
        items = []
        for name, p in iter_files(BASE):
            try:
                st = p.stat()
            except OSError:
                continue
            items.append((name, st.st_mtime_ns, st.st_size))
        return hash((tuple(items), self.bot.db.conn.total_changes))

    @tasks.loop(seconds=INTERVAL)
    async def watcher(self):
        """Runs every INTERVAL (30) seconds: the first run is the start backup, every later one is periodic."""
        if not BACKUP_WEBHOOK:
            return
        reason = "start" if self._first else "interval"
        await self.refresh("online", reason)

    @watcher.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @watcher.error
    async def _err(self, e):
        log.error("backup watcher crashed: %r", e)
        await asyncio.sleep(30)
        if not self.watcher.is_running():
            self.watcher.restart()

    # ---------- core ----------
    async def refresh(self, status: str = "online", reason: str = "manual") -> bool:
        if not BACKUP_WEBHOOK:
            return False
        async with self._lock:
            try:
                self._first = False
                self.store.flush()
                snap = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
                snap.close()
                counter = int(self.cfg.get("count", 0)) + 1
                meta = {"bot": str(self.bot.user), "created": discord.utils.utcnow().isoformat(),
                        "reason": REASONS.get(reason, reason), "status": status, "backup_number": counter}
                try:
                    self.bot.db.snapshot(snap.name)
                    cfg_export = self.export_config()
                    info = await asyncio.to_thread(build_zip, BASE, snap.name, meta, cfg_export)
                    info["summary"] = cfg_export["summary"] if cfg_export else []
                    info["vc_lines"] = cfg_export["vc_lines"] if cfg_export else []
                finally:
                    Path(snap.name).unlink(missing_ok=True)
                self._fp = self.fingerprint()
                await self._push(status, reason, info, counter)
                self._last_push = time.monotonic()
                self.last_ok = time.time()
                self.last_error = None
                if counter == 1 or reason in ("start", "manual"):
                    log.info("backup #%d sent to webhook (%s)", counter, REASONS.get(reason, reason))
                return True
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.error("backup failed: %s", self.last_error, exc_info=True)
                self._last_push = time.monotonic()
                return False

    def export_config(self) -> Optional[dict]:
        """Readable server config (names resolved from the discord cache). Never allowed to break the backup."""
        try:
            try:
                with open(AFK_FILE, "r", encoding="utf-8") as f:
                    afk = json.load(f)
            except (OSError, ValueError):
                afk = {}
            return export.collect(self.bot, afk)
        except Exception as e:
            log.warning("config export failed (backup continues without it): %r", e, exc_info=True)
            return None

    def changes(self, files: dict) -> list[str]:
        if not self._prev_files:
            return []
        out = []
        for n, (size, h) in sorted(files.items()):
            old = self._prev_files.get(n)
            if old is None:
                out.append(f"+ {n} ({human(size)})")
            elif old[1] != h:
                out.append(f"~ {n} ({human(old[0])} → {human(size)})")
        for n in sorted(set(self._prev_files) - set(files)):
            out.append(f"- {n}")
        return out

    def embed(self, status, reason, info, counter, changed: list[str], zip_size, skipped: bool) -> discord.Embed:
        b, db = self.bot, self.bot.db.counts()
        online = status == "online"
        files = info["files"]
        now = int(time.time())
        data_files = [f"`{n}` - {human(s)}" for n, (s, _) in sorted(files.items()) if n.startswith("data/")]
        code = sum(1 for n in files if n.endswith(".py"))
        assets = sum(1 for n in files if n.startswith("assets/"))
        e = discord.Embed(
            title=f"Leafy Backup #{counter}", color=GREEN if online else 0xED4245,
            timestamp=discord.utils.utcnow(),
            description=(f"**Status:** {'Online' if online else 'Offline - bot stopped'}\n"
                         f"**Reason:** {REASONS.get(reason, reason)}\n"
                         f"**Backed up:** <t:{now}:F> (<t:{now}:R>)"
                         + ("\nThe zip is too large to upload - only this status was sent." if skipped else "")))
        e.add_field(name="Package", inline=False, value=(
            f"**Files:** {len(files)} (code `{code}` | assets `{assets}` | data `{len(data_files)}`)\n"
            f"**Zip size:** {human(zip_size)} | **Unpacked:** {human(info['raw_total'])}\n"
            f"**Includes:** every bot file, `.env` (token + webhook), `data/` folder (db, VC / self-role / AFK data, banner), `BACKUP_MANIFEST.json`\n"
            f"**Readable export:** `export/SERVER_CONFIG.md` + `.json` (every channel / role setting with names), "
            f"`export/custom_vc_channels.json` (join-to-create + panel channels), "
            f"`export/db_dump.json` (every database row), `export/players_<server>.csv`\n"
            f"**Per server:** `export/servers/<server id>/` = config + saved data + every database row of that server"))
        if info.get("vc_lines"):
            e.add_field(name="Custom VC channels saved", inline=False,
                        value="\n".join(info["vc_lines"][:6])[:1024])
        if info.get("summary"):
            e.add_field(name="Server setup saved", inline=False,
                        value="\n".join(info["summary"][:5])[:1024])
        e.add_field(name="Data files", inline=False, value="\n".join(data_files)[:1024] or "none")
        if changed:
            more = f"\n... and {len(changed) - 12} more" if len(changed) > 12 else ""
            e.add_field(name=f"Changes since last backup ({len(changed)})", inline=False,
                        value="```diff\n" + "\n".join(changed[:12]) + more + "\n```")
        else:
            e.add_field(name="Changes since last backup", inline=False,
                        value="No file changes" if self._prev_files else "First backup of this session")
        e.add_field(name="Bot data", inline=True, value=(
            f"Servers: `{len(b.guilds)}`\nUsers (points): `{db['users']}`\nShop items: `{db['items']}`\n"
            f"Purchases: `{db['purchases']}`\nCustom VCs: `{len(self.store.data['vcs'])}`"))
        e.add_field(name="Bot", inline=True, value=(
            f"Latency: `{b.latency * 1000:.0f} ms`\nUptime: `{self.fmt_up()}`\nPython: `{platform.python_version()}`\n"
            f"discord.py: `{discord.__version__}`\nMode: `{BACKUP_MODE}`"))
        keys = info["env_keys"]
        e.add_field(name=".env", inline=False, value=(
            f"Keys: {', '.join(f'`{k}`' for k in keys) if keys else 'no .env found'}\n"
            f"Removed from backup: {', '.join(f'`{k}`' for k in info['env_removed']) or 'nothing (token + webhook are in the zip)'}"))
        e.set_footer(text=f"Automatic backup every {INTERVAL} seconds | also when the bot starts and stops | $backup refresh = now")
        return e

    def fmt_up(self) -> str:
        s = int(time.time() - self.started)
        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        return " ".join(x for x in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s") if x)

    async def _push(self, status, reason, info, counter):
        data = info["zip"]
        skipped = len(data) > WEBHOOK_LIMIT
        embed = self.embed(status, reason, info, counter, self.changes(info["files"]), len(data), skipped)
        wh = discord.Webhook.from_url(BACKUP_WEBHOOK, session=self.session)
        avatar = self.bot.user.display_avatar.url
        try:                                       # every backup = new message with the FULL zip attached
            fkw = {} if skipped else {"file": discord.File(io.BytesIO(data), filename="leafy_backup.zip")}
            m = await wh.send(embed=embed, wait=True, username="Leafy Backup", avatar_url=avatar, **fkw)
        except discord.HTTPException as e:
            if e.status != 413:
                raise
            m = await wh.send(embed=self.embed(status, reason, info, counter, [], len(data), True), wait=True,
                              username="Leafy Backup", avatar_url=avatar)
        old = self.cfg.get("webhook_message")
        self.cfg["webhook_message"] = m.id
        if BACKUP_MODE != "keep" and old and old != m.id:      # keep the channel clean: only the latest backup stays
            try:
                await wh.delete_message(old)
            except discord.HTTPException:
                pass
        if not skipped:
            self._last_digest = info["digest"]
        self._prev_files = info["files"]
        self.cfg["count"] = counter
        self.store.save_soon()

    # ---------- $backup (bot owner only) ----------
    @commands.group(name="backup", invoke_without_command=True)
    @checks.is_owner()
    async def backup(self, ctx: commands.Context):
        """Backup status. `$backup refresh` = back up right now (owner only)."""
        lines = [
            f"{I.s('link')} **Webhook:** "
            + ("configured (`BACKUP_WEBHOOK` in .env)" if BACKUP_WEBHOOK
               else "**not set** — add `BACKUP_WEBHOOK=https://discord.com/api/webhooks/...` to `.env` and restart"),
            f"{I.s('gift_box')} **Backups sent:** {self.cfg.get('count', 0)}",
            f"{I.s('clock')} **Last backup:** {f'<t:{int(self.last_ok)}:R>' if self.last_ok else 'not yet'}",
            f"{I.s('bolt')} **Triggers:** automatic every {INTERVAL} seconds · on bot start · on bot stop · `$backup refresh`",
            f"{I.s('settings')} **Mode:** `{BACKUP_MODE}` (`BACKUP_MODE=keep` in .env = don't delete the previous backup message)",
        ]
        if self.last_error:
            lines.append(f"{I.s('warning')} **Last error:** `{self.last_error[:200]}`")
        await ctx.reply(embed=discord.Embed(title="Backup", description="\n".join(lines), color=BLURPLE),
                        mention_author=False)

    @backup.command(name="config")
    @checks.is_owner()
    async def backup_config(self, ctx: commands.Context):
        """Send the readable config of this server (SERVER_CONFIG.md) and the players list (CSV)."""
        async with ctx.typing():
            exp = self.export_config()
            if exp is None:
                raise VCError("Couldn't build the config export - see the bot log.")
            files = [discord.File(io.BytesIO(exp["md"]), filename="SERVER_CONFIG.md")]
            if ctx.guild:
                extras = await asyncio.to_thread(self._players_csv, ctx.guild.id, exp["names"])
                if extras:
                    files.append(discord.File(io.BytesIO(extras), filename=f"players_{ctx.guild.id}.csv"))
        await ctx.reply(ok("Everything the bot has saved for the servers + the player list:"), files=files,
                        mention_author=False)

    def _players_csv(self, gid: int, names: dict) -> Optional[bytes]:
        snap = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        snap.close()
        try:
            self.bot.db.snapshot(snap.name)
            return export.db_extras(snap.name, names).get(f"export/players_{gid}.csv")
        finally:
            Path(snap.name).unlink(missing_ok=True)

    @backup.command(name="refresh")
    @checks.is_owner()
    async def backup_refresh(self, ctx: commands.Context):
        """Back up right now."""
        if not BACKUP_WEBHOOK:
            raise VCError("Add `BACKUP_WEBHOOK=...` to `.env` and restart the bot first.")
        async with ctx.typing():
            good = await self.refresh("online", "manual")
        await ctx.reply(ok("Backup sent to the webhook.") if good else f"{I.s('cross')} Backup failed: `{self.last_error}`",
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(Backup(bot))
