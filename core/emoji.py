"""Emoji helpers for $webhook and $ei.

Animated emojis work WITHOUT Nitro because the bot / webhook sends the message, not the member.
In a message you can write
    123456789012345678        a bare emoji ID
    :name:                    the plain name Discord sends for external emojis
    <a:name:123456789012345678> / <:name:123...>   already a full emoji (the animated flag is fixed if it is wrong)
and `resolve_text` turns all of them into the real `<a:name:id>` / `<:name:id>` code.

Lookup order for an ID: emojis of every server the bot is in -> the bot's own application emojis ->
the Discord CDN (works for emojis of servers the bot is NOT in; the name is unknown then, which Discord doesn't need).
"""
import logging
import re
import time
from typing import Optional

import aiohttp
import discord

log = logging.getLogger("kon.emoji")

CUSTOM = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>")
NAME = re.compile(r"(?<![\w<:]):([A-Za-z0-9_]{2,32}):(?!\d)")
BARE_ID = re.compile(r"(?<![\w<:@#&!/.=?-])(\d{17,20})(?![\w>])")      # not part of a mention / link / <:name:id>
MAX_PROBES = 8                                                      # CDN look-ups per message (unknown IDs only)

_app_cache = {"t": 0.0, "items": []}
_probe_cache: dict[int, Optional[tuple]] = {}


async def app_emojis(bot) -> list:
    """The bot's own application emojis (cached for 2 minutes)."""
    if time.monotonic() - _app_cache["t"] < 120:
        return _app_cache["items"]
    try:
        _app_cache["items"] = await bot.fetch_application_emojis()
    except Exception:
        pass
    _app_cache["t"] = time.monotonic()
    return _app_cache["items"]


async def _probe_cdn(eid: int) -> Optional[tuple]:
    """(name, animated) for an emoji the bot can't see, or None. Animated = the .gif has more than one frame."""
    if eid in _probe_cache:
        return _probe_cache[eid]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(f"https://cdn.discordapp.com/emojis/{eid}.gif?size=32") as r:
                if r.status == 200:
                    data = await r.read()
                    if data[:3] == b"GIF" and data.count(b"\x21\xf9\x04") > 1:
                        _probe_cache[eid] = ("emoji", True)
                        return _probe_cache[eid]
            async with s.get(f"https://cdn.discordapp.com/emojis/{eid}.png?size=32") as r:
                if r.status == 200:
                    _probe_cache[eid] = ("emoji", False)
                    return _probe_cache[eid]
                if r.status == 404:
                    _probe_cache[eid] = None          # really doesn't exist - don't ask again
    except (aiohttp.ClientError, TimeoutError):
        pass
    return None


async def find_emoji(bot, eid: int, probe: bool = True) -> Optional[tuple]:
    """(name, animated) or None."""
    em = bot.get_emoji(eid)
    if em is not None:
        return em.name, em.animated
    em = next((e for e in await app_emojis(bot) if e.id == eid), None)
    if em is not None:
        return em.name, em.animated
    return await _probe_cdn(eid) if probe else None


async def find_by_name(bot, name: str, guild: Optional[discord.Guild] = None):
    """Emoji object by name (this server first, exact case first)."""
    low = name.lower()
    pool = [e for e in bot.emojis if e.name.lower() == low and e.is_usable()]
    pool.sort(key=lambda e: (guild is None or e.guild_id != guild.id, e.name != name))
    if pool:
        return pool[0]
    return next((e for e in await app_emojis(bot) if e.name.lower() == low), None)


def code(name: str, animated: bool, eid: int) -> str:
    return f"<{'a' if animated else ''}:{name}:{eid}>"


async def resolve_text(bot, text: str, guild: Optional[discord.Guild] = None) -> tuple[str, int]:
    """-> (text with real emoji codes, how many emojis were filled in / fixed)."""
    changed = 0

    # 1) full codes: fix the animated flag when we know the emoji (a static code for an animated emoji shows a still image)
    known: dict[int, tuple] = {}
    for m in CUSTOM.finditer(text):
        eid = int(m.group(3))
        if eid not in known:
            hit = await find_emoji(bot, eid, probe=False)
            if hit:
                known[eid] = hit

    def fix_custom(m: re.Match) -> str:
        nonlocal changed
        eid = int(m.group(3))
        if eid in known:
            name, animated = known[eid]
            new = code(name, animated, eid)
            if new != m.group(0):
                changed += 1
            return new
        return m.group(0)

    text = CUSTOM.sub(fix_custom, text)

    # 2) :name:
    by_name: dict[str, str] = {}
    for m in NAME.finditer(text):
        n = m.group(1)
        if n not in by_name:
            em = await find_by_name(bot, n, guild)
            if em is not None:
                by_name[n] = str(em)

    def sub_name(m: re.Match) -> str:
        nonlocal changed
        hit = by_name.get(m.group(1))
        if hit:
            changed += 1
            return hit
        return m.group(0)

    text = NAME.sub(sub_name, text)

    # 3) bare IDs
    by_id: dict[str, str] = {}
    probes = 0
    for m in BARE_ID.finditer(text):
        raw = m.group(1)
        if raw in by_id:
            continue
        eid = int(raw)
        hit = await find_emoji(bot, eid, probe=False)
        if hit is None and probes < MAX_PROBES:
            probes += 1
            hit = await _probe_cdn(eid)
        if hit:
            by_id[raw] = code(hit[0], hit[1], eid)

    def sub_id(m: re.Match) -> str:
        nonlocal changed
        hit = by_id.get(m.group(1))
        if hit:
            changed += 1
            return hit
        return m.group(0)

    text = BARE_ID.sub(sub_id, text)
    return text, changed
