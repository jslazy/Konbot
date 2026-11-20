"""Custom icons = Application Emojis.

On startup every icon in USED is uploaded once (as `kon_<name>`) from assets/icons.
If an upload fails the bot falls back to a unicode emoji, so nothing breaks.
"""
import asyncio
import hashlib
import json
import logging

import discord

from .config import DATA_DIR, ICON_DIR

log = logging.getLogger("kon.icons")
EMOJI_PREFIX = "kon_"
HASH_FILE = DATA_DIR / "icon_hashes.json"      # which image (sha1) is uploaded for every kon_<name> emoji


def _load_hashes() -> dict:
    try:
        return json.loads(HASH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_hashes(d: dict):
    try:
        HASH_FILE.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
    except OSError as e:
        log.warning("could not save icon hashes: %s", e)

FALLBACK = {
    "lock": "🔒", "unlock": "🔓", "hide": "🙈", "show": "👁️", "logout": "🚪", "ban": "🚫",
    "user_add": "➕", "users": "👥", "signal": "📶", "edit": "✏️", "volume": "🔊", "crown": "👑",
    "check": "✅", "cross": "❌", "hourglass": "⏳", "clock": "⏱️", "info": "ℹ️", "mute": "🔇",
    "swap": "🔁", "left": "◀️", "right": "▶️", "speaker": "🔊", "mic": "🎙️", "settings": "⚙️",
    "warning": "⚠️", "tools": "🛠️", "bolt": "⚡", "refresh": "🔄", "shield_check": "🛡️",
    "user_minus": "➖", "leaf": "🍃", "member": "👤", "headset": "🎧", "help": "❓",
    "check_outline": "✅", "cross_filled": "❌", "home": "🏠", "menu": "📋", "sparkles": "✨",
    "stopwatch": "⏱️", "plus": "➕", "minus": "➖", "trophy": "🏆", "coins": "🪙", "wallet": "💰", "gift_box": "📦",
    "heart": "💛", "message": "💬", "mic": "🎙️",
    "play": "▶️", "terminal": "💻", "star_fill": "⭐", "gift": "🎁", "link": "🔗",
    "image": "🖼️", "palette": "🎨", "upload": "📤", "tag": "🏷️", "diamond": "💠",     # self-role editor
    "heart_broken": "🖤", "skull": "💀", "stop": "🛑", "repeat": "🔁",
    "fire": "🔥", "gamepad": "🎮", "target": "🎯", "folder": "📁", "user_star": "🌟", "grid": "🔲",   # aura / games / info
    "rock": "🪨", "paper": "📄", "scissors": "✂️", "tap": "👆", "team_red": "🔴", "team_blue": "🔵",     # Handfight / TimeTap
    "light_red": "🔴", "light_green": "🟢", "shield_star": "🛡️", "swap_small": "⚔️", "thumbs_up": "👍",
}

# every icon used anywhere in the bot (kept small: emoji uploads are rate limited)
USED = list(FALLBACK.keys() - {"member"})


class Icons:
    def __init__(self):
        self.map: dict[str, discord.Emoji] = {}
        self.refreshed = False                           # True when icons were re-uploaded (emoji IDs changed)

    async def load(self, bot: discord.Client):
        try:
            existing = {e.name: e for e in await bot.fetch_application_emojis()}
        except Exception as e:  # old discord.py / no permission
            log.warning("Could not fetch application emojis (%s) - using unicode fallbacks", e)
            return
        hashes = _load_hashes()
        changed = False
        for name in USED:
            key = EMOJI_PREFIX + name
            path = ICON_DIR / f"{name}.png"
            data = path.read_bytes() if path.exists() else None
            digest = hashlib.sha1(data).hexdigest() if data else None
            cur = existing.get(key)
            if cur is not None and (data is None or hashes.get(name) == digest):
                self.map[name] = cur                     # up to date
                continue
            if data is None:
                continue
            if cur is not None:                          # stale image on Discord (e.g. the old green icon) -> replace it
                try:
                    await cur.delete()
                    await asyncio.sleep(0.4)
                except Exception as e:
                    log.warning("could not delete old icon %s: %s", key, e)
                    self.map[name] = cur
                    continue
            try:
                self.map[name] = await bot.create_application_emoji(name=key, image=data)
                hashes[name] = digest
                changed = True
                self.refreshed = True
                log.info("uploaded icon %s", key)
                await asyncio.sleep(0.7)
            except Exception as e:
                log.warning("icon upload failed for %s: %s", key, e)
        if changed:
            _save_hashes(hashes)
        log.info("icons ready: %d/%d custom", len(self.map), len(USED))

    def s(self, name: str) -> str:
        """icon as text (for embeds / messages)"""
        e = self.map.get(name)
        return str(e) if e else FALLBACK.get(name, "▫️")

    def e(self, name: str):
        """icon for buttons / selects"""
        return self.map.get(name) or FALLBACK.get(name)


I = Icons()


def ok(text: str) -> str:
    return f"{I.s('check')} {text}"


def err(text: str) -> str:
    return f"{I.s('cross')} {text}"
