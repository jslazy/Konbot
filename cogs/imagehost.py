"""Image host - turn any image / GIF / banner into a link. The files live in ONE private storage channel; only the
LINK (+ the hosting message id) is saved in Kon's data, so backups stay small and safe.

(Admins = Manage Server, the bot owner or a $konextra owner)
$imgchannel [#channel]      set the storage channel (no channel = this one)      $imgchannelreset  remove it
$tolink [name] [url]        attach an image / GIF, reply to a message with one, or give a link -> saved link
                            (aliases: $link, $imglink)   up to 5 files at once
$links [page]               list the saved links
$linkget <name>             fresh link + preview of a saved image
$linkdel <name>             delete a saved image (also deletes the hosting message)

Discord's CDN links carry an expiry stamp, so a link you paste somewhere else stops working after a while. That is why
the hosting message id is kept: `$linkget <name>` (and every Kon feature that uses a saved image) asks Discord for a
FRESH link each time. Other cogs: `bot.get_cog("ImageHost").fresh_url(guild, name)` / `.resolve_image(guild, text)`.
"""
import asyncio
import io
import logging
import re
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import checks
from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, ok
from core.ui import replied_message

log = logging.getLogger("kon.imagehost")

KEY = "imagehost"
MAX_FILES = 5
MAX_ITEMS = 300
HARD_LIMIT = 25 * 1024 * 1024
PER_PAGE = 8
UA = {"User-Agent": "Mozilla/5.0 (Kon bot)"}
URL_RE = re.compile(r"https?://[^\s<>]+")
IMG_URL = re.compile(r"\.(?:png|jpe?g|gif|webp)(?:\?|$)", re.I)
NO_PING = discord.AllowedMentions.none()


def sniff_ext(data: bytes) -> Optional[str]:
    """Real image type from the file bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:32]


def fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


class ImageHost(commands.Cog):
    """Image / GIF / banner -> saved link."""

    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------ data
    def cfg(self, gid: int) -> dict:
        c = self.bot.db.get_settings(gid, KEY)
        c.setdefault("channel", None)
        c.setdefault("items", {})
        return c

    def save(self, gid: int, c: dict):
        self.bot.db.set_settings(gid, KEY, c)

    def storage_channel(self, guild: discord.Guild) -> discord.TextChannel:
        cid = self.cfg(guild.id)["channel"]
        ch = guild.get_channel(cid) if cid else None
        if not isinstance(ch, discord.TextChannel):
            raise VCError(f"No image channel yet. An admin can set one with `{PREFIX}imgchannel #channel`.")
        p = ch.permissions_for(guild.me)
        if not (p.view_channel and p.send_messages and p.attach_files):
            raise VCError(f"I need **View Channel, Send Messages and Attach Files** in {ch.mention}.")
        return ch

    # ------------------------------------------------------------ API for other features
    async def fresh_url(self, guild: discord.Guild, name: str) -> Optional[str]:
        """A working link for a saved image (asks Discord for a fresh one and remembers it)."""
        c = self.cfg(guild.id)
        it = c["items"].get(slug(name))
        if not it:
            return None
        ch = guild.get_channel(it["c"])
        if ch is not None:
            try:
                msg = await ch.fetch_message(it["m"])
                if msg.attachments and msg.attachments[0].url != it.get("url"):
                    it["url"] = msg.attachments[0].url
                    self.save(guild.id, c)
            except discord.HTTPException:
                pass
        return it.get("url")

    async def resolve_image(self, guild: discord.Guild, text: str) -> str:
        """A link, or the NAME of a saved image -> a usable link."""
        text = (text or "").strip()
        if text.startswith(("http://", "https://")):
            return text
        url = await self.fresh_url(guild, text)
        if not url:
            raise VCError(f"`{text}` is not a link and not a saved image name (see `{PREFIX}links`).")
        return url

    # ------------------------------------------------------------ download helpers
    async def download(self, url: str, limit: int) -> bytes:
        if not url.startswith(("http://", "https://")):
            raise VCError("Links must start with `https://`.")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=UA) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        raise VCError("I couldn't open that link. It may have expired.")
                    data = bytearray()
                    async for chunk in r.content.iter_chunked(65536):
                        data += chunk
                        if len(data) > limit:
                            raise VCError(f"That image is over **{limit // (1024 * 1024)} MB**.")
                    return bytes(data)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise VCError("I couldn't download that link.")

    async def gather(self, ctx: commands.Context, image: Optional[discord.Attachment], urls: list[str]) -> list:
        """Everything that can be converted: ("att", Attachment) / ("url", str). Own message first, then the replied one."""
        out: list = []
        if image is not None:
            out.append(("att", image))
        if ctx.interaction is None:                                   # prefix: all attachments of the message
            out += [("att", a) for a in ctx.message.attachments if image is None or a.id != image.id]
        ref = await replied_message(ctx) if ctx.interaction is None else None
        if ref is not None:
            out += [("att", a) for a in ref.attachments]
            for e in ref.embeds:
                u = e.image.url or (e.thumbnail.url if e.type == "image" else None)
                if u:
                    out.append(("url", u))
            out += [("url", u) for u in URL_RE.findall(ref.content or "") if IMG_URL.search(u)]
        out += [("url", u) for u in urls]
        seen, uniq = set(), []
        for kind, v in out:
            key = v.id if kind == "att" else v
            if key not in seen:
                seen.add(key)
                uniq.append((kind, v))
        return uniq[:MAX_FILES]

    # ------------------------------------------------------------ commands
    @commands.hybrid_command(name="imgchannel", aliases=["setimg", "imagechannel"],
                             description="(Admin) Set the private channel that stores converted images")
    @app_commands.describe(channel="Storage channel (default: this one)")
    @checks.admin_or_owner()
    async def imgchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            raise VCError("Pick a normal text channel.")
        p = channel.permissions_for(ctx.guild.me)
        if not (p.view_channel and p.send_messages and p.attach_files):
            raise VCError(f"I need **View Channel, Send Messages and Attach Files** in {channel.mention}.")
        c = self.cfg(ctx.guild.id)
        c["channel"] = channel.id
        self.save(ctx.guild.id, c)
        await ctx.reply(ok(f"Images are stored in {channel.mention} now. Keep that channel private and don't delete "
                           f"its messages.\nConvert with `{PREFIX}tolink` · list with `{PREFIX}links`."),
                        mention_author=False)

    @commands.hybrid_command(name="imgchannelreset", description="(Admin) Remove the image storage channel setting")
    @checks.admin_or_owner()
    async def imgchannelreset(self, ctx: commands.Context):
        c = self.cfg(ctx.guild.id)
        if not c["channel"]:
            raise VCError("No image channel was set.")
        c["channel"] = None
        self.save(ctx.guild.id, c)
        await ctx.reply(ok("Image channel removed. Saved links keep working; new uploads need a new channel."),
                        mention_author=False)

    @commands.hybrid_command(name="tolink", aliases=["link", "imglink"],
                             description="(Admin) Turn an image / GIF / banner into a saved link")
    @app_commands.describe(name="Name to save it under (optional)", image="Image or GIF file", url="...or a link to an image")
    @checks.admin_or_owner()
    async def tolink(self, ctx: commands.Context, name: Optional[str] = None,
                     image: Optional[discord.Attachment] = None, url: Optional[str] = None):
        """$tolink [name] + attach a file / reply to a message with an image / $tolink name https://link"""
        urls = []
        if name and name.startswith(("http://", "https://")):
            urls.append(name)
            name = None
        if url and url.startswith(("http://", "https://")):
            urls.append(url)
        sources = await self.gather(ctx, image, urls)
        if not sources:
            raise VCError(f"Attach an image / GIF, reply to a message that has one, or give a link: "
                          f"`{PREFIX}tolink banner https://...`")
        ch = self.storage_channel(ctx.guild)
        c = self.cfg(ctx.guild.id)
        base = slug(name) if name else ""
        if name and not base:
            raise VCError("Use letters, numbers, `-` or `_` for the name.")
        limit = min(ctx.guild.filesize_limit, HARD_LIMIT)

        results = []
        async with ctx.typing():
            for i, (kind, src) in enumerate(sources, 1):
                if kind == "att":
                    if src.size > limit:
                        raise VCError(f"**{src.filename}** is over **{limit // (1024 * 1024)} MB**.")
                    data = await src.read()
                else:
                    data = await self.download(src, limit)
                ext = sniff_ext(data)
                if ext is None:
                    raise VCError("Only PNG, JPG, GIF or WEBP images work.")
                key = base if base and len(sources) == 1 else (f"{base}-{i}" if base else self.auto_name(c))
                replaced = key in c["items"]
                if not replaced and len(c["items"]) >= MAX_ITEMS:
                    raise VCError(f"The list is full ({MAX_ITEMS}). Delete some with `{PREFIX}linkdel <name>`.")
                msg = await ch.send(f"`{key}` · saved by {ctx.author} (storage - don't delete)",
                                    file=discord.File(io.BytesIO(data), filename=f"{key}.{ext}"),
                                    allowed_mentions=NO_PING)
                old = c["items"].get(key)
                c["items"][key] = {"c": msg.channel.id, "m": msg.id, "url": msg.attachments[0].url, "t": ext,
                                   "s": len(data), "by": ctx.author.id, "ts": int(time.time())}
                self.save(ctx.guild.id, c)
                if old:
                    await self.forget(ctx.guild, old)
                results.append((key, ext, len(data), msg.attachments[0].url, replaced))

        e = discord.Embed(color=BRAND, title=f"{I.s('link')} Saved link" + ("s" if len(results) > 1 else ""))
        e.description = "\n".join(
            f"**{k}** · {ext.upper()} · {fmt_size(size)}" + (" · *replaced the old one*" if rep else "") + f"\n```{u}```"
            for k, ext, size, u, rep in results)
        if len(results) == 1:
            e.set_image(url=results[0][3])
        e.set_footer(text=f"Stored in #{ch.name} · {PREFIX}linkget <name> gives a fresh link any time")
        await ctx.reply(embed=e, mention_author=False)

    @staticmethod
    def auto_name(c: dict) -> str:
        n = len(c["items"]) + 1
        while f"img-{n}" in c["items"]:
            n += 1
        return f"img-{n}"

    async def forget(self, guild: discord.Guild, it: Optional[dict]):
        """Delete the hosting message (best effort)."""
        if not it:
            return
        ch = guild.get_channel(it.get("c", 0))
        if ch is not None:
            try:
                await ch.get_partial_message(it["m"]).delete()
            except discord.HTTPException:
                pass

    @commands.hybrid_command(name="links", aliases=["imglist", "linklist"], description="List the saved image links")
    @app_commands.describe(page="Page number")
    @checks.admin_or_owner()
    async def links(self, ctx: commands.Context, page: int = 1):
        c = self.cfg(ctx.guild.id)
        items = sorted(c["items"].items())
        ch = f"<#{c['channel']}>" if c["channel"] else f"*not set* - `{PREFIX}imgchannel #channel`"
        if not items:
            raise VCError(f"Nothing saved yet. Storage channel: {ch.replace('*', '')}. Use `{PREFIX}tolink`.")
        pages = max(1, -(-len(items) // PER_PAGE))
        page = min(max(page, 1), pages)
        chunk = items[(page - 1) * PER_PAGE: page * PER_PAGE]
        lines = [f"**{k}** · {it['t'].upper()} · {fmt_size(it.get('s', 0))} · <@{it['by']}> · <t:{it['ts']}:R>"
                 for k, it in chunk]
        e = discord.Embed(color=BLURPLE, title=f"{I.s('image')} Saved images ({len(items)})", description="\n".join(lines))
        e.add_field(name="Storage channel", value=ch, inline=False)
        e.set_footer(text=f"Page {page}/{pages} · {PREFIX}linkget <name> · {PREFIX}linkdel <name>")
        await ctx.reply(embed=e, mention_author=False)

    @commands.hybrid_command(name="linkget", aliases=["getlink"], description="Fresh link + preview of a saved image")
    @app_commands.describe(name="Saved image name")
    @checks.admin_or_owner()
    async def linkget(self, ctx: commands.Context, name: str):
        url = await self.fresh_url(ctx.guild, name)
        if not url:
            raise VCError(f"No saved image called `{slug(name)}`. See `{PREFIX}links`.")
        e = discord.Embed(color=BRAND, title=f"{I.s('link')} {slug(name)}", description=f"```{url}```")
        e.set_image(url=url)
        await ctx.reply(embed=e, mention_author=False)

    @commands.hybrid_command(name="linkdel", aliases=["dellink"], description="(Admin) Delete a saved image")
    @app_commands.describe(name="Saved image name")
    @checks.admin_or_owner()
    async def linkdel(self, ctx: commands.Context, name: str):
        c = self.cfg(ctx.guild.id)
        it = c["items"].pop(slug(name), None)
        if it is None:
            raise VCError(f"No saved image called `{slug(name)}`.")
        self.save(ctx.guild.id, c)
        await self.forget(ctx.guild, it)
        await ctx.reply(ok(f"**{slug(name)}** deleted."), mention_author=False)


async def setup(bot):
    await bot.add_cog(ImageHost(bot))
