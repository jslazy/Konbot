"""Self roles - a banner + role list + toggle buttons message (Components V2).

$selfrole setup   open the editor (add / edit / remove roles, banner, title, footer, colour, all emojis)
$setsr [#channel] choose the channel where the self-role message lives (posts it there)
$editsr           same editor.  Attach / reply to an image or GIF to use it as the banner,
                  or press the Banner button and upload a file (or paste a link).
"""
import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from core import ratelimit
from core.config import BLURPLE, DATA_DIR, GREEN, PREFIX
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import FieldsModal, OwnedView, replied_message, to_partial

log = logging.getLogger("leafy.selfroles")

MAX_ROLES = 20                                  # 5 buttons per row, 4 rows (Discord allows 40 components / message)
MAX_BANNER = 4 * 1024 * 1024                    # keeps the automatic backup (10 MB webhook limit) small
MAX_BANNER_MB = MAX_BANNER // (1024 * 1024)
BANNER_DIR = DATA_DIR / "selfrole"
BANNER_DIR.mkdir(exist_ok=True)
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
           "footer": "Click the buttons below to toggle your roles.", "color": GREEN, "banner": None, "roles": []}


def emoji_or_none(text: str) -> Optional[str]:
    """Emoji the user typed -> value we store (custom emoji / unicode emoji / None)."""
    text = (text or "").strip()
    if not text:
        return None
    if CUSTOM_EMOJI.fullmatch(text):
        return text
    if len(text) > 16 or text.isascii():          # plain words / ':name:' are not emojis
        raise VCError(f"`{text}` isn't an emoji. Use a normal emoji (🔥) or a custom one from a server I'm in.")
    return text


def risky(role: discord.Role) -> bool:
    p = role.permissions
    return p.administrator or p.manage_roles or p.manage_guild or p.manage_channels or p.ban_members or p.kick_members


class RoleButton(discord.ui.Button):
    def __init__(self, entry: dict):
        super().__init__(style=discord.ButtonStyle.secondary, label=entry["label"][:80] or None,
                         emoji=to_partial(entry.get("emoji")), custom_id=f"leafy:sr:{entry['role']}")
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
        cog, gid = self.editor.cog, self.editor.guild.id
        try:
            files = list(self.upload.values)
            link = (self.link.value or "").strip()
            if files:
                att = files[0]
                if att.size > MAX_BANNER:
                    raise VCError(f"That file is bigger than **{MAX_BANNER_MB} MB**. Please use a smaller one.")
                cog.store_banner(gid, await att.read())
            elif link:
                await cog.set_banner_url(gid, link)
            else:
                raise VCError("Upload a file or paste a link first (use **Remove banner** to delete it).")
        except VCError as e:
            return await interaction.followup.send(err(str(e)), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(err("I couldn't read that file. Please try again."), ephemeral=True)
        await self.editor.saved(interaction, "Banner updated.")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("banner modal failed", exc_info=error)
        text = err("Something went wrong while saving the banner.")
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
            f"{I.s('message')} **Channel:** {ch.mention if ch else f'not set - use `{PREFIX}setsr #channel`'}\n"
            f"{I.s('image')} **Banner:** {self.cog.banner_info(cfg)}\n"
            f"{I.s('edit')} **Title:** {title}\n"
            f"{I.s('info')} **Footer:** {cfg['footer'] or '*none*'}\n"
            f"{I.s('palette')} **Colour:** `#{cfg['color']:06X}`\n\n"
            f"**Roles ({len(cfg['roles'])}/{MAX_ROLES})**\n" + ("\n".join(rows) or "*none yet - press **Add role***")))
        e.set_footer(text=f"Changes update the posted message automatically · {PREFIX}editsr + image/GIF = new banner")
        return e

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
            return await interaction.response.send_message(err(f"You can add at most {MAX_ROLES} roles."), ephemeral=True)
        view = discord.ui.View(timeout=120)
        sel = discord.ui.RoleSelect(placeholder="Select the role members can toggle...")

        async def picked(i: discord.Interaction):
            role = sel.values[0]
            if any(r["role"] == role.id for r in cfg["roles"]):
                return await i.response.send_message(err("That role is already in the list."), ephemeral=True)
            if not role.is_assignable():
                return await i.response.send_message(
                    err("I can't give that role. Move my role above it (and it can't be @everyone / a bot role)."),
                    ephemeral=True)
            if risky(role):
                return await i.response.send_message(
                    err("That role has powerful permissions (Administrator / Manage ...). "
                        "It can't be a self role."), ephemeral=True)

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
        await interaction.response.send_message("Which role should members be able to toggle?", view=view, ephemeral=True)

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
            return await interaction.response.send_message(err("There are no roles yet."), ephemeral=True)
        await interaction.response.send_message("Pick a role to edit (button text, line text and both emojis):",
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
            return await interaction.response.send_message(err("There are no roles yet."), ephemeral=True)
        await interaction.response.send_message("Pick the roles to remove:", view=view, ephemeral=True)

    # ----- look of the message -----
    @discord.ui.button(label="Banner", style=discord.ButtonStyle.secondary, row=1)
    async def banner_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ratelimit.guard_interaction(interaction, "modal"):
            return
        await interaction.response.send_modal(BannerModal(self))

    @discord.ui.button(label="Remove banner", style=discord.ButtonStyle.secondary, row=1)
    async def banner_off_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)
        if not cfg["banner"]:
            return await interaction.response.send_message(err("There is no banner."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        self.cog.drop_banner(self.guild.id)
        await self.saved(interaction, "Banner removed.")

    @discord.ui.button(label="Title", style=discord.ButtonStyle.secondary, row=1)
    async def title_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.cfg(self.guild.id)

        async def cb(i: discord.Interaction, v):
            if not v[0]:
                raise VCError("The title can't be empty.")
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
                    view, _ = self.build_view(self.cfg(int(gid)), with_file=False)
                    self.bot.add_view(view, message_id=c["message"])
                except Exception as e:
                    log.warning("could not restore self-role buttons for %s: %r", gid, e)

    # ---------- banner ----------
    def banner_path(self, cfg: dict) -> Optional[Path]:
        if not cfg.get("banner"):
            return None
        p = BANNER_DIR / cfg["banner"]
        return p if p.exists() else None

    def drop_banner(self, gid: int):
        c = self.cfg(gid)
        if c["banner"]:
            (BANNER_DIR / c["banner"]).unlink(missing_ok=True)
        c["banner"] = None
        self.save()

    def banner_info(self, cfg: dict) -> str:
        p = self.banner_path(cfg)
        if p is None:
            return "*none*" + (" (file missing - upload it again)" if cfg.get("banner") else "")
        kb = p.stat().st_size / 1024
        size = f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
        return f"{I.s('check')} set · `{p.suffix[1:].upper()}` · {size}"

    def store_banner(self, gid: int, data: bytes):
        """Validate (real file type from the bytes) and save the banner. Replaces the old one."""
        ext = sniff_ext(data)
        if ext is None:
            raise VCError("The banner must be a PNG, JPG, GIF or WEBP image (a direct image file, not a web page).")
        if len(data) > MAX_BANNER:
            raise VCError(f"That image is bigger than **{MAX_BANNER_MB} MB**. Please use a smaller one.")
        c = self.cfg(gid)
        name = f"{gid}.{ext}"
        tmp = BANNER_DIR / (name + ".tmp")
        tmp.write_bytes(data)                                   # write first, so a failed save never loses the old banner
        if c["banner"] and c["banner"] != name:
            (BANNER_DIR / c["banner"]).unlink(missing_ok=True)
        tmp.replace(BANNER_DIR / name)
        c["banner"] = name
        self.save()

    async def set_banner_url(self, gid: int, url: str):
        if not url.startswith(("http://", "https://")):
            raise VCError("Please give an image link starting with `https://`.")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                             headers={"User-Agent": "Mozilla/5.0 (Leafy bot)"}) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        raise VCError("I couldn't download that image (the link may have expired).")
                    if (r.content_length or 0) > MAX_BANNER:
                        raise VCError(f"That image is bigger than **{MAX_BANNER_MB} MB**. Please use a smaller one.")
                    data = bytearray()
                    async for chunk in r.content.iter_chunked(64 * 1024):   # read the WHOLE file (read(n) can return a partial chunk)
                        data += chunk
                        if len(data) > MAX_BANNER:
                            raise VCError(f"That image is bigger than **{MAX_BANNER_MB} MB**. Please use a smaller one.")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise VCError("I couldn't download that image.")
        self.store_banner(gid, bytes(data))

    # ---------- the message ----------
    def build_view(self, cfg: dict, with_file: bool = True):
        """-> (LayoutView, File | None)"""
        items, file = [], None
        banner = self.banner_path(cfg)
        if banner:
            name = f"banner_{int(time.time())}{banner.suffix}"      # new name per post -> Discord never shows a stale/blank image
            file = discord.File(banner, filename=name) if with_file else None
            items += [discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{name}")),
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
                row.add_item(RoleButton(entry))
            view.add_item(row)
        return view, file

    async def publish(self, guild: discord.Guild) -> discord.Message:
        cfg = self.cfg(guild.id)
        ch = guild.get_channel(cfg["channel"]) if cfg["channel"] else None
        if not isinstance(ch, discord.TextChannel):
            raise VCError(f"Set the channel first: `{PREFIX}setsr #channel`.")
        if not cfg["roles"]:
            raise VCError("Add at least one role first.")
        perms = ch.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.attach_files):
            raise VCError(f"I need **View Channel, Send Messages and Attach Files** in {ch.mention}.")
        try:
            msg = None
            if cfg["message"]:
                view, file = self.build_view(cfg)
                try:
                    msg = await ch.get_partial_message(cfg["message"]).edit(
                        view=view, attachments=[file] if file else [])
                except discord.NotFound:
                    msg = None
            if msg is None:
                view, file = self.build_view(cfg)               # fresh File object
                msg = await ch.send(view=view, files=[file] if file else [],
                                    allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            raise VCError(f"Discord refused the message: {e.text}\nCheck your emojis and the banner image.")
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
            return await interaction.followup.send(err("That role isn't available any more."), ephemeral=True)
        if not guild.me.guild_permissions.manage_roles or not role.is_assignable() or risky(role):
            return await interaction.followup.send(
                err("I can't manage that role right now. Please tell an admin."), ephemeral=True)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self role")
                text = f"{I.s('minus')} Removed {role.mention}."
            else:
                await member.add_roles(role, reason="Self role")
                text = ok(f"You now have {role.mention}.")
        except discord.HTTPException:
            text = err("Something went wrong. Please try again.")
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
                raise VCError(f"That image is bigger than **{MAX_BANNER_MB} MB**. Please use a smaller one.")
            self.store_banner(ctx.guild.id, await img.read())
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
            "Members click a button to get or remove a role.\n\n"
            f"`{PREFIX}selfrole setup` · open the editor (roles, banner, title, footer, emojis)\n"
            f"`{PREFIX}setsr #channel` · choose where the message is posted\n"
            f"`{PREFIX}editsr` · edit it later (attach an image = new banner)"))
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
            return await ctx.reply(ok(f"Self-role channel set to {channel.mention}.\n"
                                      f"Now add roles with `{PREFIX}selfrole setup` - the message posts automatically."),
                                   mention_author=False)
        async with ctx.typing():
            msg = await self.publish(ctx.guild)
        await ctx.reply(ok(f"Self roles posted in {channel.mention}: {msg.jump_url}"), mention_author=False)

    @commands.command(name="editsr", aliases=["esr"])
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def editsr(self, ctx: commands.Context):
        """$editsr - edit the self-role message (attach or reply to an image to change the banner)"""
        await self.open_editor(ctx)


async def setup(bot):
    await bot.add_cog(SelfRoles(bot))
