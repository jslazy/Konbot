"""Custom icons = Application Emojis.

On startup every icon in USED is uploaded once (as `kon_<name>`) from assets/icons.
If an upload fails the bot falls back to a unicode emoji, so nothing breaks.
"""
import asyncio
import logging

import discord

from .config import ICON_DIR

log = logging.getLogger("kon.icons")
EMOJI_PREFIX = "kon_"

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

    async def load(self, bot: discord.Client):
        try:
            existing = {e.name: e for e in await bot.fetch_application_emojis()}
        except Exception as e:  # old discord.py / no permission
            log.warning("Could not fetch application emojis (%s) - using unicode fallbacks", e)
            return
        for name in USED:
            key = EMOJI_PREFIX + name
            if key in existing:
                self.map[name] = existing[key]
                continue
            path = ICON_DIR / f"{name}.png"
            if not path.exists():
                continue
            try:
                self.map[name] = await bot.create_application_emoji(name=key, image=path.read_bytes())
                log.info("uploaded icon %s", key)
                await asyncio.sleep(0.7)
            except Exception as e:
                log.warning("icon upload failed for %s: %s", key, e)
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
