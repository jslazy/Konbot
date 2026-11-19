"""Shared image upload helper: every place that used to ask for an image LINK now has an upload button too.

    view = ...; await interaction.response.send_modal(ImageModal(bot, guild, "Banner", on_done, allow_clear=True))

`on_done(interaction, item)` gets `item = {"url": str, "ref": {"c": channel id, "m": message id} | None}` (or None when the
person pressed "remove" / left it empty with allow_clear). Uploaded files are hosted ONCE (in the bot owner's image channel
when `$imgchannel` is set, otherwise in the channel where the button was pressed) and only the link + the hosting message
id are saved - so the settings stay small and `fresh()` can always ask Discord for a working link.
"""
import asyncio
import io
import logging
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiohttp
import discord

from .errors import VCError
from .icons import err

log = logging.getLogger("kon.media")
MAX_MB = 8
MAX_BYTES = MAX_MB * 1024 * 1024
UA = {"User-Agent": "Mozilla/5.0 (Kon bot)"}
NO_PING = discord.AllowedMentions.none()


def sniff_ext(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def download(url: str, limit: int = MAX_BYTES) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise VCError("Links must start with `https://`.")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=UA) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    raise VCError("I couldn't open that link. It may have expired.")
                data = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) > limit:
                        raise VCError(f"That image is over **{limit // (1024 * 1024)} MB**.")
                return bytes(data)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise VCError("I couldn't download that link.")


async def host_bytes(bot, guild: discord.Guild, data: bytes, label: str, fallback=None) -> dict:
    """Upload the image once and return {"url", "ref"}."""
    ext = sniff_ext(data)
    if ext is None:
        raise VCError("The file must be a PNG, JPG, GIF or WEBP image.")
    if len(data) > MAX_BYTES:
        raise VCError(f"That image is over **{MAX_MB} MB**.")
    note = f"{label} · {guild.name} ({guild.id}) (storage - don't delete)"
    filename = f"{label.lower().replace(' ', '_')}_{guild.id}.{ext}"
    host = bot.get_cog("ImageHost")
    if host is not None and host.host_ready():
        msg = await host.host_upload(data, filename, note)
    else:
        ch = fallback
        if ch is None or not hasattr(ch, "send"):
            raise VCError("I have no channel to host the image. The bot owner can set one with `$imgchannel #channel`.")
        try:
            msg = await ch.send(note, file=discord.File(io.BytesIO(data), filename=filename), allowed_mentions=NO_PING)
        except discord.HTTPException:
            raise VCError("I couldn't upload the image here (need Attach Files). The bot owner can set a host with `$imgchannel`.")
    return {"url": msg.attachments[0].url, "ref": {"c": msg.channel.id, "m": msg.id}}


async def from_link(bot, guild: discord.Guild, url: str, label: str, fallback=None) -> dict:
    """A pasted link -> item. Discord CDN links expire, so those are downloaded and re-hosted."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise VCError("Give an image link starting with `https://`.")
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(("discordapp.com", "discordapp.net", "discord.com")):
        return await host_bytes(bot, guild, await download(url), label, fallback)
    head = b""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=UA) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    raise VCError("I couldn't open that link.")
                head = await r.content.read(32)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise VCError("I couldn't open that link.")
    if sniff_ext(head) is None:
        raise VCError("That link isn't a direct PNG / JPG / GIF / WEBP image.")
    return {"url": url, "ref": None}


async def fresh(bot, item: Optional[dict]) -> Optional[str]:
    """A working link for a saved item (Discord CDN links expire; the hosting message gives a new one)."""
    if not item:
        return None
    ref = item.get("ref")
    if ref:
        ch = bot.get_channel(ref.get("c", 0))
        if ch is not None:
            try:
                msg = await ch.fetch_message(ref["m"])
                if msg.attachments and msg.attachments[0].url != item.get("url"):
                    item["url"] = msg.attachments[0].url
            except discord.HTTPException:
                pass
    return item.get("url")


async def forget(bot, item: Optional[dict]):
    """Delete the hosting message of an item that is replaced / removed (best effort)."""
    ref = (item or {}).get("ref")
    if not ref:
        return
    ch = bot.get_channel(ref.get("c", 0))
    if ch is not None:
        try:
            await ch.get_partial_message(ref["m"]).delete()
        except discord.HTTPException:
            pass


class ImageModal(discord.ui.Modal):
    """Upload an image / GIF - or paste a link. `on_done(interaction, item_or_None)`; the interaction is already deferred
    (ephemeral), answer with interaction.followup."""

    def __init__(self, bot, guild: discord.Guild, title: str, on_done: Callable[[discord.Interaction, Optional[dict]], Awaitable],
                 *, label: str = "Image", allow_clear: bool = False, fallback=None, upload_only: bool = False):
        super().__init__(title=title[:45], timeout=300)
        self.bot, self.guild, self.on_done = bot, guild, on_done
        self.label, self.allow_clear, self.fallback, self.upload_only = label, allow_clear, fallback, upload_only
        need = upload_only and not allow_clear                # upload-only without "remove" = a file is a must
        self.upload = discord.ui.FileUpload(required=need, min_values=1 if need else 0, max_values=1)
        self.link = discord.ui.TextInput(placeholder="https://...", max_length=1000, required=False)
        hint = f"PNG, JPG, GIF or WEBP · max {MAX_MB} MB" + (" · empty = remove" if (upload_only and allow_clear) else "")
        self.add_item(discord.ui.Label(text="Upload image or GIF", description=hint, component=self.upload))
        if not upload_only:                                   # upload-only pickers have no link field
            self.add_item(discord.ui.Label(text="...or paste an image link" + (" (empty = remove)" if allow_clear else ""),
                                           component=self.link))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            files = list(self.upload.values)
            link = "" if self.upload_only else (self.link.value or "").strip()
            fb = self.fallback or interaction.channel
            if files:
                att = files[0]
                if att.size > MAX_BYTES:
                    raise VCError(f"That file is over **{MAX_MB} MB**.")
                item = await host_bytes(self.bot, self.guild, await att.read(), self.label, fb)
            elif link:
                item = await from_link(self.bot, self.guild, link, self.label, fb)
            elif self.allow_clear:
                item = None
            else:
                raise VCError("Upload an image or GIF." if self.upload_only else "Upload a file or paste a link.")
            await self.on_done(interaction, item)
        except VCError as e:
            await interaction.followup.send(err(str(e)), ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send(err("I couldn't read or upload that file. Try again."), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("image modal failed", exc_info=error)
        text = err("Couldn't save the image.")
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
