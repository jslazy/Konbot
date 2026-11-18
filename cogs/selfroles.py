"""Self roles - a banner + role list + toggle buttons message (Components V2).

$selfrole setup   open the editor (add / edit / remove roles, banner, title, footer, colour, all emojis)
$setsr [#channel] choose the channel where the self-role message lives (posts it there)
$editsr           same editor.  Attach / reply to an image or GIF to use it as the banner,
                  or press the Banner button and upload a file (or paste a link).

Banner storage: the image/GIF itself is NOT kept in data/ any more. Only a LINK is saved in the data
(cfg["banner_url"], plus cfg["banner_ref"] = the Discord message that hosts an uploaded file, so a fresh link can
always be fetched - Discord CDN links expire).  Set ASSET_CHANNEL_ID in .env to host uploads in a private channel.
"""
import asyncio
import io
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands

from core import ratelimit
from core.config import ASSET_CHANNEL_ID, BLURPLE, DATA_DIR, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import FieldsModal, OwnedView, replied_message, to_partial

log = logging.getLogger("kon.selfroles")

MAX_ROLES = 20                                  # 5 buttons per row, 4 rows (Discord allows 40 components / message)
MAX_BANNER = 8 * 1024 * 1024                    # Discord upload limit for the hosting message (no longer part of the backup)
MAX_BANNER_MB = MAX_BANNER // (1024 * 1024)
BANNER_DIR = DATA_DIR / "selfrole"               # OLD place of banner files - only read once to migrate them to a link
CUSTOM_EMOJI = re.compile(r"<a?:\w{2,32}:\d{15,25}>")


def sniff_ext(data: bytes) -> Optional[str]:
    """Detect the real image type from the file bytes (content-types / file names are often wrong)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


DEFAULT = {"channel": None, "message": None, "title": "Self Roles :", "title_emoji": "🍃",
           "footer": "Click the buttons below to toggle your roles.", "color": BRAND, "banner": None, "banner_url": None, "banner_ref": None, "roles": []}


def emoji_or_none(text: str) -> Optional[str]:
    """Emoji the user typed -> value we store (custom emoji / unicode emoji / None)."""
    text = (text or "").strip()
    if not text:
        return None
    if CUSTOM_EMOJI.fullmatch(text):
        return text
    if len(text) > 16 or text.isascii():          # plain words / ':name:' are not emojis
        raise VCError(f"`{text}` isn't an emoji. Use a normal one (🔥) or one from a server I'm in.")
    return text


def risky(role: discord.Role) -> bool:
    p = role.permissions
    return p.administrator or p.manage_roles or p.manage_guild or p.manage_channels or p.ban_members or p.kick_members


class RoleButton(discord.ui.Button):
    def __init__(self, entry: dict, prefix: str = "kon"):      # prefix "leafy" = buttons of panels posted before the rebrand
        super().__init__(style=discord.ButtonStyle.secondary, label=entry["label"][:80] or None,
                         emoji=to_partial(entry.get("emoji")), custom_id=f"{prefix}:sr:{entry['role']}")
        self.role_id = entry["role"]

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("SelfRoles")
        if cog:
            await cog.toggle(interaction, self.role_id)


class BannerModal(discord.ui.Modal):
    """Upload a banner (PNG / JPG / GIF / WEBP) or paste an image link."""

    def __init__(self, editor: "SelfRoleEditor"):
        super().__init__(title="Banner image", timeout=300)
        self.editor = editor
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.link = discord.ui.TextInput(placeholder="https://...", max_length=1000, required=False)
        self.add_item(discord.ui.Label(text="Upload image or GIF", description=f"PNG, JPG, GIF or WEBP · max {MAX_BANNER_MB} MB",
                                       component=self.upload))
        self.add_item(discord.ui.Label(text="...or an image link", component=self.link))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog = self.editor.cog
        try:
            files = list(self.upload.values)
            link = (self.link.value or "").strip()
            if files:
                att = files[0]
                if att.size > MAX_BANNER:
                    raise VCError(f"That file is over **{MAX_BANNER_MB} MB**.")
                await cog.store_banner(self.editor.guild, await att.read(), self.editor.fallback_channel())
            elif link:
                await cog.set_banner_url(self.editor.guild, link, self.editor.fallback_channel())
            else:
                raise VCError("Upload a file or paste a link. (**Remove banner** deletes it.)")
        except VCError as e:
            return await interaction.followup.send(err(str(e)), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(err("I couldn't read that file. Try again."), ephemeral=True)
        await self.editor.saved(interaction, "Banner updated.")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("banner modal failed", exc_info=error)
        text = err("Couldn't save the banner.")
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class SelfRoleEditor(OwnedView):
    """$editsr panel."""

    def __init__(self, cog: "SelfRoles", ctx: commands.Context):
        super().__init__(ctx.author.id, timeout=900)
        self.cog, self.guild = cog, ctx.guild
        # custom (application) emojis from assets/icons - no normal unicode emojis on the buttons
        for btn, icon in ((self.add_btn, "plus"), (self.edit_btn, "edit"), (self.remove_btn, "user_minus"),
                          (self.banner_btn, "image"), (self.banner_off_btn, "cross"), (self.title_btn, "tag"),
                          (self.footer_btn, "menu"), (self.colour_btn, "palette"),
                          (self.publish_btn, "upload"), (self.close_btn, "logout")):
            btn.emoji = I.e(icon)

    # ----- display -----
    def embed(self) -> discord.Embed:
        cfg = self.cog.cfg(self.guild.id)
        ch = self.guild.get_channel(cfg["channel"]) if cfg["channel"] else None
        rows = []
        for n, r in enumerate(cfg["roles"], 1):
            role = self.guild.get_role(r["role"])
            rows.append(f"`{n}.` {r.get('line_emoji') or I.s('diamond')} **{r['line']}** → "
                        f"{role.mention if role else '*deleted role*'}\n"
                        f"      button: {r.get('emoji') or ''} {r['label']}")
        title = f"{cfg['title_emoji'] + ' ' if cfg['title_emoji'] else ''}{cfg['title']}"
        e = discord.Embed(title=f"{I.s('settings')} Self Roles - Editor", color=cfg["color"], description=(
            f"{I.s('message')} **Channel:** {ch.mention if ch else f'not set (`{PREFIX}setsr #channel`)'}\n"
            f"{I.s('image')} **Banner:** {self.cog.banner_info(cfg)}\n"
            f"{I.s('edit')} **Title:** {title}\n"
            f"{I.s('info')} **Footer:** {cfg['footer'] or '*none*'}\n"
            f"{I.s('palette')} **Colour:** `#{cfg['color']:06X}`\n\n"
            f"**Roles ({len(cfg['roles'])}/{MAX_ROLES})**\n" + ("\n".join(rows) or "*none yet. Tap **Add role***")))
        e.set_footer(text=f"The posted message updates by itself · {PREFIX}editsr + image/GIF = new banner")
        return e

    def fallback_channel(self):
        return getattr(getattr(self, "message", None), "channel", None)

    async def refresh(self):
        if self.message:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass

    async def saved(self, interaction: discord.Interaction, text: str):
        """Save done -> update the posted message + this panel, then confirm (ephemeral)."""
        warn = await self.cog.sync(self.guild)
        msg = ok(text) + (f"\n{I.s('warning')} {warn}" if warn else "")
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        await self.refresh()

    # ----- role entries -----
    def _entry_modal(self, title: str, entry: dict, callback):
        return FieldsModal(title[:45], [
            ("Button text", entry["label"], 40, True),
            ("Button emoji (optional)", entry.get("emoji") or "", 64, False),
            ("Line text in the message", entry["line"], 60, True),
            ("Line emoji in the message (optional)", entry.get("line_emoji") or "", 64, False)], callback)

    @discord.ui.button(label="Add role", style=discord.ButtonStyle.success, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)
        if len(cfg["roles"]) >= MAX_ROLES:
            return await interaction.response.send_message(err(f"Max {MAX_ROLES} roles."), ephemeral=True)
        view = discord.ui.View(timeout=120)
        sel = discord.ui.RoleSelect(placeholder="Pick the role...")

        async def picked(i: discord.Interaction):
            role = sel.values[0]
            if any(r["role"] == role.id for r in cfg["roles"]):
                return await i.response.send_message(err("Already in the list."), ephemeral=True)
            if not role.is_assignable():
                return await i.response.send_message(
                    err("I can't give that role. Move my role above it."),
                    ephemeral=True)
            if risky(role):
                return await i.response.send_message(
                    err("That role has strong permissions, so it can't be a self role."), ephemeral=True)

            async def cb(i2: discord.Interaction, v):
                entry = {"role": role.id, "label": v[0], "emoji": emoji_or_none(v[1]),
                         "line": v[2], "line_emoji": emoji_or_none(v[3])}
                cfg["roles"].append(entry)
                self.cog.save()
                await self.saved(i2, f"{role.mention} added.")
            default = {"label": role.name[:40], "emoji": None, "line": role.name[:60], "line_emoji": None}
            await i.response.send_modal(self._entry_modal(f"Add: {role.name}", default, cb))
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Which role?", view=view, ephemeral=True)

    def _pick_view(self, placeholder: str, handler, multi: bool = False):
        cfg = self.cog.cfg(self.guild.id)
        if not cfg["roles"]:
            return None
        opts = []
        for n, r in enumerate(cfg["roles"]):
            role = self.guild.get_role(r["role"])
            opts.append(discord.SelectOption(label=(role.name if role else "deleted role")[:100], value=str(n),
                                             description=r["line"][:100], emoji=to_partial(r.get("emoji"))))
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder=placeholder, options=opts[:25], max_values=len(opts[:25]) if multi else 1)

        async def picked(i: discord.Interaction):
            await handler(i, [int(x) for x in sel.values])
        sel.callback = picked
        view.add_item(sel)
        return view

    @discord.ui.button(label="Edit role", style=discord.ButtonStyle.primary, row=0)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)

        async def handler(i: discord.Interaction, idx: list):
            entry = cfg["roles"][idx[0]]

            async def cb(i2: discord.Interaction, v):
                entry.update(label=v[0], emoji=emoji_or_none(v[1]), line=v[2], line_emoji=emoji_or_none(v[3]))
                self.cog.save()
                await self.saved(i2, "Role updated.")
            await i.response.send_modal(self._entry_modal("Edit role", entry, cb))
        view = self._pick_view("Which role do you want to edit?", handler)
        if view is None:
            return await interaction.response.send_message(err("No roles yet."), ephemeral=True)
        await interaction.response.send_message("Pick a role to edit:",
                                                view=view, ephemeral=True)

    @discord.ui.button(label="Remove role", style=discord.ButtonStyle.danger, row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)

        async def handler(i: discord.Interaction, idx: list):
            cfg["roles"] = [r for n, r in enumerate(cfg["roles"]) if n not in idx]
            self.cog.save()
            await i.response.edit_message(content=ok("Removed."), view=None)
            warn = await self.cog.sync(self.guild)
            if warn:
                await i.followup.send(f"{I.s('warning')} {warn}", ephemeral=True)
            await self.refresh()
        view = self._pick_view("Which roles should be removed?", handler, multi=True)
        if view is None:
            return await interaction.response.send_message(err("No roles yet."), ephemeral=True)
        await interaction.response.send_message("Pick roles to remove:", view=view, ephemeral=True)

    # ----- look of the message -----
    @discord.ui.button(label="Banner", style=discord.ButtonStyle.secondary, row=1)
    async def banner_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ratelimit.guard_interaction(interaction, "modal"):
            return
        await interaction.response.send_modal(BannerModal(self))

    @discord.ui.button(label="Remove banner", style=discord.ButtonStyle.secondary, row=1)
    async def banner_off_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)
        if not self.cog.has_banner(cfg):
            return await interaction.response.send_message(err("No banner to remove."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.cog.drop_banner(self.guild)
        await self.saved(interaction, "Banner removed.")

    @discord.ui.button(label="Title", style=discord.ButtonStyle.secondary, row=1)
    async def title_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)

        async def cb(i: discord.Interaction, v):
            if not v[0]:
                raise VCError("Title can't be empty.")
            cfg["title"], cfg["title_emoji"] = v[0], emoji_or_none(v[1])
            self.cog.save()
            await self.saved(i, "Title updated.")
        await interaction.response.send_modal(FieldsModal("Title", [
            ("Title", cfg["title"], 80, True), ("Title emoji (optional)", cfg["title_emoji"] or "", 64, False)], cb))

    @discord.ui.button(label="Footer", style=discord.ButtonStyle.secondary, row=1)
    async def footer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)

        async def cb(i: discord.Interaction, v):
            cfg["footer"] = v[0]
            self.cog.save()
            await self.saved(i, "Footer updated.")
        await interaction.response.send_modal(FieldsModal("Footer text", [
            ("Text under the roles (empty = none)", cfg["footer"], 300, False)], cb))

    @discord.ui.button(label="Colour", style=discord.ButtonStyle.secondary, row=1)
    async def colour_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)

        async def cb(i: discord.Interaction, v):
            h = v[0].lstrip("#")
            if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
                raise VCError("Use a hex colour like `#F5C518`.")
            cfg["color"] = int(h, 16)
            self.cog.save()
            await self.saved(i, "Colour updated.")
        await interaction.response.send_modal(FieldsModal("Side colour", [
            ("Hex colour", f"#{cfg['color']:06X}", 7, True)], cb))

    # ----- publish / close -----
    @discord.ui.button(label="Post / update message", style=discord.ButtonStyle.primary, row=2)
    async def publish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ratelimit.guard_interaction(interaction, "modal"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await self.cog.publish(self.guild)
        except VCError as e:
            return await interaction.followup.send(err(str(e)), ephemeral=True)
        await interaction.followup.send(ok(f"Posted: {msg.jump_url}"), ephemeral=True)
        await self.refresh()

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=2)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=None)
        self.stop()


class SelfRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- data ----------
    def all(self) -> dict:
        return self.bot.store.data.setdefault("selfroles", {})

    def cfg(self, gid: int) -> dict:
        c = self.all().setdefault(str(gid), {})
        for k, v in DEFAULT.items():
            c.setdefault(k, list(v) if isinstance(v, list) else v)
        return c

    def save(self):
        self.bot.store.save_soon()

    async def cog_load(self):
        # make the buttons of every posted message work again after a restart
        for gid, c in list(self.all().items()):
            if c.get("message") and c.get("roles"):
                try:
                    view = self.build_view(self.cfg(int(gid)))
                    self.bot.add_view(view, message_id=c["message"])
                    self.bot.add_view(self.build_view(self.cfg(int(gid)), prefix="leafy"), message_id=c["message"])   # old panels
                except Exception as e:
                    log.warning("could not restore self-role buttons for %s: %r", gid, e)

    @commands.Cog.listener()
    async def on_ready(self):
        """Old banner FILES (data/selfrole/*.gif ...) become a link once, then the file is deleted - so backups carry
        links, not images."""
        for gid, c in list(self.all().items()):
            if c.get("banner") and not (c.get("banner_url") or c.get("banner_ref")):
                guild = self.bot.get_guild(int(gid))
                if guild is not None:
                    await self.migrate_legacy_banner(guild)

    # ---------- banner (stored as a LINK only) ----------
    def has_banner(self, cfg: dict) -> bool:
        return bool(cfg.get("banner_url") or cfg.get("banner_ref") or self.legacy_path(cfg))

    def legacy_path(self, cfg: dict) -> Optional[Path]:
        """Old versions kept the file in data/selfrole/. It is moved to a link on the next publish."""
        if not cfg.get("banner"):
            return None
        p = BANNER_DIR / cfg["banner"]
        return p if p.exists() else None

    def banner_info(self, cfg: dict) -> str:
        if cfg.get("banner_ref"):
            return f"{I.s('check')} set · hosted in Discord (only the link is saved)"
        if cfg.get("banner_url"):
            return f"{I.s('check')} set · link saved"
        if self.legacy_path(cfg):
            return f"{I.s('check')} set · old file (will turn into a link on the next update)"
        return "*none*"

    def host_channel(self, guild: discord.Guild, fallback=None) -> discord.abc.Messageable:
        """Where uploaded banners are hosted: ASSET_CHANNEL_ID (private storage channel) > self-role channel > here."""
        for cid in (ASSET_CHANNEL_ID, self.cfg(guild.id).get("channel")):     # only used when the owner has no image host
            ch = guild.get_channel(cid) if cid else None
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages \
                    and ch.permissions_for(guild.me).attach_files:
                return ch
        if fallback is not None:
            return fallback
        raise VCError("I need a channel to host the image. Set `ASSET_CHANNEL_ID` in `.env` or run `"
                      f"{PREFIX}setsr #channel` first.")

    async def _forget_host(self, guild: discord.Guild, ref: Optional[dict]):
        """Delete the old hosting message (best effort)."""
        if not ref:
            return
        ch = self.bot.get_channel(ref.get("c", 0))            # the owner's image host (another server) or an old local channel
        if ch:
            try:
                await ch.get_partial_message(ref["m"]).delete()
            except discord.HTTPException:
                pass

    async def store_banner(self, guild: discord.Guild, data: bytes, fallback=None):
        """Validate the bytes (real file type), upload them ONCE to the hosting channel and keep only the link."""
        ext = sniff_ext(data)
        if ext is None:
            raise VCError("Banner must be a PNG, JPG, GIF or WEBP image file.")
        if len(data) > MAX_BANNER:
            raise VCError(f"That image is over **{MAX_BANNER_MB} MB**.")
        c = self.cfg(guild.id)
        host = self.bot.get_cog("ImageHost")
        note = f"Self-role banner · {guild.name} ({guild.id}) (storage - don't delete)"
        if host is not None and host.host_ready():            # the bot owner's image host channel (works for EVERY server)
            msg = await host.host_upload(data, f"banner_{guild.id}.{ext}", note)
        else:
            ch = self.host_channel(guild, fallback)
            msg = await ch.send(note, file=discord.File(io.BytesIO(data), filename=f"banner_{guild.id}.{ext}"))
        old = c.get("banner_ref")
        c["banner_ref"] = {"c": msg.channel.id, "m": msg.id}
        c["banner_url"] = msg.attachments[0].url
        if c.get("banner"):                                     # old local file is not needed any more
            (BANNER_DIR / c["banner"]).unlink(missing_ok=True)
            c["banner"] = None
        self.save()
        await self._forget_host(guild, old)

    async def _probe_image(self, url: str) -> bytes:
        """Download only the first bytes to check the link is a real image. Returns those bytes."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                             headers={"User-Agent": "Mozilla/5.0 (Kon bot)"}) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        raise VCError("I couldn't open that link. It may have expired.")
                    return await r.content.read(32)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise VCError("I couldn't open that link.")

    async def set_banner_url(self, guild: discord.Guild, url: str, fallback=None):
        if not url.startswith(("http://", "https://")):
            raise VCError("Give an image link starting with `https://`.")
        host = (urlparse(url).hostname or "").lower()
        if host.endswith(("discordapp.com", "discordapp.net", "discord.com")):
            # Discord links expire after a while -> download once and re-host it, so the saved link keeps working
            data = bytearray()
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                                 headers={"User-Agent": "Mozilla/5.0 (Kon bot)"}) as s:
                    async with s.get(url) as r:
                        if r.status != 200:
                            raise VCError("I couldn't download that image. The link may have expired.")
                        async for chunk in r.content.iter_chunked(64 * 1024):
                            data += chunk
                            if len(data) > MAX_BANNER:
                                raise VCError(f"That image is over **{MAX_BANNER_MB} MB**.")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise VCError("I couldn't download that image.")
            return await self.store_banner(guild, bytes(data), fallback)
        head = await self._probe_image(url)
        if sniff_ext(head) is None:
            raise VCError("That link isn't a direct PNG / JPG / GIF / WEBP image.")
        c = self.cfg(guild.id)
        old = c.get("banner_ref")
        c["banner_url"], c["banner_ref"] = url, None             # a normal link: just save the link, nothing else
        if c.get("banner"):
            (BANNER_DIR / c["banner"]).unlink(missing_ok=True)
            c["banner"] = None
        self.save()
        await self._forget_host(guild, old)

    async def drop_banner(self, guild: discord.Guild):
        c = self.cfg(guild.id)
        await self._forget_host(guild, c.get("banner_ref"))
        if c.get("banner"):
            (BANNER_DIR / c["banner"]).unlink(missing_ok=True)
        c["banner"] = c["banner_url"] = c["banner_ref"] = None
        self.save()

    async def migrate_legacy_banner(self, guild: discord.Guild):
        """Old local banner file -> upload once, keep the link, delete the file (so data/ and backups stay small)."""
        c = self.cfg(guild.id)
        p = self.legacy_path(c)
        if p is None or c.get("banner_ref") or c.get("banner_url"):
            return
        try:
            await self.store_banner(guild, p.read_bytes())
            log.info("self-role banner of %s moved from a file to a link", guild.id)
        except (VCError, discord.HTTPException, OSError) as e:
            log.warning("could not migrate the banner of %s: %r", guild.id, e)

    async def banner_link(self, guild: discord.Guild, cfg: dict) -> Optional[str]:
        """The link to show. For Discord-hosted uploads a fresh link is fetched from the hosting message."""
        ref = cfg.get("banner_ref")
        if ref:
            ch = self.bot.get_channel(ref.get("c", 0))
            if ch:
                try:
                    msg = await ch.fetch_message(ref["m"])
                    if msg.attachments:
                        if msg.attachments[0].url != cfg.get("banner_url"):
                            cfg["banner_url"] = msg.attachments[0].url
                            self.save()
                except discord.HTTPException:
                    pass
        return cfg.get("banner_url")

    # ---------- the message ----------
    def build_view(self, cfg: dict, banner_url: Optional[str] = None, prefix: str = "kon"):
        """-> LayoutView. The banner is shown from a link (nothing is uploaded with the message)."""
        items = []
        if banner_url:
            items += [discord.ui.MediaGallery(discord.MediaGalleryItem(banner_url)),
                      discord.ui.Separator()]
        head = f"{cfg['title_emoji'] + ' ' if cfg['title_emoji'] else ''}__{cfg['title']}__"
        items += [discord.ui.TextDisplay(f"## {head}"), discord.ui.Separator()]
        lines = [f"{r['line_emoji'] + ' ' if r.get('line_emoji') else ''}**{r['line']}**" for r in cfg["roles"]]
        items.append(discord.ui.TextDisplay("\n".join(lines) or "*No roles yet.*"))
        if cfg["footer"]:
            quote = "\n".join(f"> {ln}" for ln in cfg["footer"].splitlines() or [cfg["footer"]])
            items += [discord.ui.Separator(), discord.ui.TextDisplay(quote)]
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*items, accent_colour=discord.Colour(cfg["color"])))
        for i in range(0, len(cfg["roles"]), 5):
            row = discord.ui.ActionRow()
            for entry in cfg["roles"][i:i + 5]:
                row.add_item(RoleButton(entry, prefix))
            view.add_item(row)
        return view

    async def publish(self, guild: discord.Guild) -> discord.Message:
        cfg = self.cfg(guild.id)
        ch = guild.get_channel(cfg["channel"]) if cfg["channel"] else None
        if not isinstance(ch, discord.TextChannel):
            raise VCError(f"Set a channel first: `{PREFIX}setsr #channel`.")
        if not cfg["roles"]:
            raise VCError("Add a role first.")
        perms = ch.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.attach_files):
            raise VCError(f"I need **View Channel, Send Messages and Attach Files** in {ch.mention}.")
        await self.migrate_legacy_banner(guild)
        url = await self.banner_link(guild, cfg)
        try:
            msg = None
            view = self.build_view(cfg, url)
            if cfg["message"]:
                try:
                    msg = await ch.get_partial_message(cfg["message"]).edit(view=view, attachments=[])
                except discord.NotFound:
                    msg = None
            if msg is None:
                msg = await ch.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            raise VCError(f"Discord said no: {e.text}\nCheck the emojis and banner.")
        cfg["message"] = msg.id
        self.save()
        return msg

    async def sync(self, guild: discord.Guild) -> Optional[str]:
        """Bring the posted message up to date after an edit. Returns a warning text or None."""
        cfg = self.cfg(guild.id)
        if not cfg["channel"]:
            return None
        if not cfg["roles"]:
            if cfg["message"]:                              # last role removed -> remove the message
                ch = guild.get_channel(cfg["channel"])
                if ch:
                    try:
                        await ch.get_partial_message(cfg["message"]).delete()
                    except discord.HTTPException:
                        pass
                cfg["message"] = None
                self.save()
            return None
        try:
            await self.publish(guild)
        except VCError as e:
            return str(e)
        except discord.HTTPException as e:
            return f"Couldn't update the message: {e.text}"
        return None

    # ---------- button click ----------
    async def toggle(self, interaction: discord.Interaction, role_id: int):
        if not await ratelimit.guard_interaction(interaction, "button"):
            return
        guild, member = interaction.guild, interaction.user
        cfg = self.cfg(guild.id)
        await interaction.response.defer(ephemeral=True)
        role = guild.get_role(role_id)
        if role is None or not any(r["role"] == role_id for r in cfg["roles"]):
            return await interaction.followup.send(err("That role is gone."), ephemeral=True)
        if not guild.me.guild_permissions.manage_roles or not role.is_assignable() or risky(role):
            return await interaction.followup.send(
                err("I can't manage that role right now. Tell an admin."), ephemeral=True)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self role")
                text = f"{I.s('minus')} Removed {role.mention}."
            else:
                await member.add_roles(role, reason="Self role")
                text = ok(f"You have {role.mention} now.")
        except discord.HTTPException:
            text = err("Something broke. Try again.")
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        cfg = self.all().get(str(role.guild.id))
        if cfg and any(r["role"] == role.id for r in cfg["roles"]):
            cfg["roles"] = [r for r in cfg["roles"] if r["role"] != role.id]
            self.save()
            await self.sync(role.guild)

    # ---------- commands ----------
    async def open_editor(self, ctx: commands.Context):
        # an image attached to the command (or to the message you reply to) becomes the banner
        msg = await replied_message(ctx)
        atts = list(ctx.message.attachments) + (list(msg.attachments) if msg else [])
        img = next((a for a in atts if (a.content_type or "").startswith("image/")
                    or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))), None)
        note = ""
        if img is not None:
            if img.size > MAX_BANNER:
                raise VCError(f"That image is over **{MAX_BANNER_MB} MB**.")
            await self.store_banner(ctx.guild, await img.read(), ctx.channel)
            note = ok("Banner updated.")
            await self.sync(ctx.guild)
        view = SelfRoleEditor(self, ctx)
        view.message = await ctx.reply(note or None, embed=view.embed(), view=view, mention_author=False)

    @commands.group(name="selfrole", aliases=["sr"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def selfrole(self, ctx: commands.Context):
        """Self roles: banner, role list and toggle buttons."""
        e = discord.Embed(title=f"{I.s('crown')} Self Roles", color=BLURPLE, description=(
            "Members tap a button to get or drop a role.\n\n"
            f"`{PREFIX}selfrole setup` · open the editor\n"
            f"`{PREFIX}setsr #channel` · pick the channel\n"
            f"`{PREFIX}editsr` · edit later (attach an image = new banner)"))
        await ctx.reply(embed=e, mention_author=False)

    @selfrole.command(name="setup")
    @commands.has_permissions(manage_guild=True)
    async def sr_setup(self, ctx: commands.Context):
        """Open the self-role editor."""
        await self.open_editor(ctx)

    @commands.command(name="setsr")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def setsr(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """$setsr [#channel] - choose the self-role channel (default: this one) and post the message there"""
        channel = channel or ctx.channel
        cfg = self.cfg(ctx.guild.id)
        if cfg["message"] and cfg["channel"] != channel.id:      # moving -> delete the old message
            old = ctx.guild.get_channel(cfg["channel"]) if cfg["channel"] else None
            if old:
                try:
                    await old.get_partial_message(cfg["message"]).delete()
                except discord.HTTPException:
                    pass
            cfg["message"] = None
        cfg["channel"] = channel.id
        self.save()
        if not cfg["roles"]:
            return await ctx.reply(ok(f"Self-role channel is {channel.mention}. Add roles with `{PREFIX}selfrole setup`."),
                                   mention_author=False)
        async with ctx.typing():
            msg = await self.publish(ctx.guild)
        await ctx.reply(ok(f"Posted in {channel.mention}: {msg.jump_url}"), mention_author=False)

    @commands.command(name="editsr", aliases=["esr"])
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def editsr(self, ctx: commands.Context):
        """$editsr - edit the self-role message (attach or reply to an image to change the banner)"""
        await self.open_editor(ctx)


async def setup(bot):
    await bot.add_cog(SelfRoles(bot))
