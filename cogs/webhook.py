"""$webhook - send messages through a webhook into any channel, edit them later. Animated emojis work WITHOUT Nitro.

(needs the Manage Webhooks permission - or the bot owner / a $konextra owner)
$webhook                       open the editor: pick the channel, write the message, build an EMBED (title, text, colour, footer,
                               image, thumbnail), set a name + avatar (upload an image or paste a link), Send, Edit
                               Everything you set is saved per server (name, avatar, embed, images) and comes back next time.
$webhook #channel text...      send straight away (multi-line text works)
$webhookedit <message link> new text      edit a message Kon's webhook sent   (alias $whe)

Buttons: the editor has Add Button / Remove Button - a link button (label + link + emoji or emoji ID) under the message.
Up to 10 buttons, saved per server like the embed.

Emojis in the text: paste the emoji ID (123456789012345678) or :name: - Kon turns it into the real animated emoji.
$ei [search | here | <emoji id>]   all animated emojis (of the servers you share with Kon) with their IDs.

Only messages sent by Kon's own webhook can be edited. The member needs View + Send permission in the target channel
and @everyone / role pings only work for members who may mention everyone.
"""
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import checks
from core import emoji as emojis
from core import media
from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import OwnedView

log = logging.getLogger("kon.webhook")

MSG_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
PER_PAGE = 10


def can_webhook():
    return checks.admin_or_owner("manage_webhooks")


class FormModal(discord.ui.Modal):
    """A modal with ready-made TextInputs; `cb(interaction, [values])`."""

    def __init__(self, title: str, inputs: list, cb):
        super().__init__(title=title, timeout=300)
        self._cb, self._inputs = cb, inputs
        for i in inputs:
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self._cb(interaction, [i.value.strip() for i in self._inputs])
        except VCError as e:
            text = err(str(e))
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)


def box(label, default=None, *, long=False, max_length=100, required=False, placeholder=None) -> discord.ui.TextInput:
    return discord.ui.TextInput(label=label, default=default or None, max_length=max_length, required=required,
                                placeholder=placeholder,
                                style=discord.TextStyle.paragraph if long else discord.TextStyle.short)


class PageView(OwnedView):
    """◀ ▶ over a list of embeds."""

    def __init__(self, invoker_id: int, pages: list):
        super().__init__(invoker_id, timeout=180)
        self.pages, self.i = pages, 0
        self.sync()

    def sync(self):
        self.prev.disabled = self.i == 0
        self.nxt.disabled = self.i >= len(self.pages) - 1

    @discord.ui.button(emoji=I.e("left"), style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _):
        self.i -= 1
        self.sync()
        await interaction.response.edit_message(embed=self.pages[self.i], view=self)

    @discord.ui.button(emoji=I.e("right"), style=discord.ButtonStyle.secondary)
    async def nxt(self, interaction: discord.Interaction, _):
        self.i += 1
        self.sync()
        await interaction.response.edit_message(embed=self.pages[self.i], view=self)


KEY = "webhook"          # per server: last name / avatar / embed / images (part of the backup)


def parse_color(text: str) -> int:
    text = (text or "").strip().lstrip("#")
    if not text:
        return BRAND
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        if len(text) != 6:
            raise ValueError
        return int(text, 16)
    except ValueError:
        raise VCError("Colour must be a hex code like `FF9B22`.")


MAX_BUTTONS = 10
KEEP = object()          # "don't touch the buttons" when editing a message


def make_view(buttons: list, use_emoji: bool = True) -> Optional[discord.ui.View]:
    """Link buttons under the message (None = no buttons). Link buttons work for webhook messages without any callback."""
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for b in buttons[:MAX_BUTTONS]:
        emoji = emojis.to_partial_checked(b.get("emoji")) if use_emoji else None
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=b.get("label") or None,
                                        url=b["url"], emoji=emoji))
    return view


def clean_url(text: str) -> str:
    text = (text or "").strip()
    if not re.match(r"https?://\S+\.\S+", text) or len(text) > 512:
        raise VCError("The link must start with `https://` (or `http://`).")
    return text


class WebhookPanel(OwnedView):
    def __init__(self, cog: "Webhook", ctx: commands.Context, channel: Optional[discord.TextChannel]):
        super().__init__(ctx.author.id, timeout=900)
        self.cog, self.guild, self.author = cog, ctx.guild, ctx.author
        self.channel = channel
        saved = cog.bot.db.get_settings(ctx.guild.id, KEY)
        self.content = ""
        self.username = saved.get("username", "")
        self.avatar = saved.get("avatar")              # item {"url","ref"} (upload / link) or None
        self.emb = dict(saved.get("embed") or {})      # title, description, color, footer, author
        self.image = saved.get("image")                # embed image item
        self.thumb = saved.get("thumb")                # embed thumbnail item
        self.buttons = [b for b in (saved.get("buttons") or []) if isinstance(b, dict) and b.get("url")]
        self.last: Optional[tuple] = None      # (channel id, message id) of the last message sent
        self.pick_channel.default_values = [channel] if channel else []

    def save(self):
        self.cog.bot.db.set_settings(self.guild.id, KEY, {
            "username": self.username, "avatar": self.avatar, "embed": self.emb, "image": self.image, "thumb": self.thumb,
            "buttons": self.buttons})

    async def build_embed(self) -> Optional[discord.Embed]:
        """The embed that Send will post (None = message only)."""
        c = self.emb
        img = await media.fresh(self.cog.bot, self.image)
        thumb = await media.fresh(self.cog.bot, self.thumb)
        if not (c.get("title") or c.get("description") or c.get("footer") or c.get("author") or img or thumb):
            return None
        title, desc = c.get("title") or None, c.get("description") or None
        if desc:
            desc, _ = await emojis.resolve_text(self.cog.bot, desc, self.guild)
        if title:
            title, _ = await emojis.resolve_text(self.cog.bot, title, self.guild)
        e = discord.Embed(title=title, description=desc, color=c.get("color", BRAND))
        if c.get("author"):
            e.set_author(name=c["author"][:256])
        if c.get("footer"):
            e.set_footer(text=c["footer"][:2048])
        if img:
            e.set_image(url=img)
        if thumb:
            e.set_thumbnail(url=thumb)
        return e

    async def embeds(self) -> list:
        preview = "*empty - press **Message** or **Embed***"
        if self.content:
            preview, _ = await emojis.resolve_text(self.cog.bot, self.content, self.guild)
            preview = preview[:3900]
        e = discord.Embed(title=f"{I.s('link')} Webhook sender", color=BLURPLE, description=preview)
        e.add_field(name="Channel", value=self.channel.mention if self.channel else "*pick one below*")
        e.add_field(name="Name", value=self.username or "*webhook default*")
        e.add_field(name="Avatar", value="uploaded / link set" if self.avatar else "*default*")
        e.add_field(name="Embed", value="set" if any(self.emb.values()) or self.image or self.thumb else "*none*")
        e.add_field(name="Embed image", value="set" if self.image else "*none*")
        e.add_field(name="Thumbnail", value="set" if self.thumb else "*none*")
        if self.buttons:
            rows = [f"`{n}.` {b.get('emoji') or ''} {b.get('label') or '*(no text)*'} → <{b['url'][:60]}>"
                    for n, b in enumerate(self.buttons, 1)]
            e.add_field(name=f"Buttons ({len(self.buttons)}/{MAX_BUTTONS})", value="\n".join(rows)[:1000], inline=False)
        else:
            e.add_field(name="Buttons", value="*none - press **Add Button***")
        if self.last:
            e.add_field(name="Last sent", value=f"https://discord.com/channels/{self.guild.id}/{self.last[0]}/{self.last[1]}",
                        inline=False)
        e.set_footer(text="Embed preview is shown below · saved per server · emoji ID or :name: works")
        out = [e]
        pe = await self.build_embed()
        if pe is not None:
            out.append(pe)
        return out

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embeds=await self.embeds(), view=self)

    async def refresh_message(self):
        if self.message:
            try:
                await self.message.edit(embeds=await self.embeds(), view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=CHANNEL_TYPES, placeholder="Channel to send to", row=0)
    async def pick_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.channel = self.guild.get_channel(select.values[0].id)
        await self.refresh(interaction)

    @discord.ui.button(label="Message", emoji=I.e("edit"), style=discord.ButtonStyle.primary, row=1)
    async def set_message(self, interaction: discord.Interaction, _):
        async def done(i: discord.Interaction, v: list):
            self.content = v[0]
            await self.refresh(i)
        await interaction.response.send_modal(FormModal("Webhook message", [
            box("Message (emoji ID or :name: works)", self.content, long=True, max_length=2000, required=False)], done))

    @discord.ui.button(label="Embed", emoji=I.e("palette"), style=discord.ButtonStyle.primary, row=1)
    async def set_embed(self, interaction: discord.Interaction, _):
        async def done(i: discord.Interaction, v: list):
            color = parse_color(v[2]) if v[2] else None
            self.emb = {"title": v[0], "description": v[1], "footer": v[3], "author": v[4]}
            if color is not None:
                self.emb["color"] = color
            self.save()
            await self.refresh(i)
        c = self.emb
        cur_color = f"{c['color']:06X}" if isinstance(c.get("color"), int) else ""
        await interaction.response.send_modal(FormModal("Embed (empty everything = no embed)", [
            box("Title", c.get("title"), max_length=256),
            box("Description", c.get("description"), long=True, max_length=4000),
            box("Colour (hex, e.g. FF9B22)", cur_color, max_length=7),
            box("Footer", c.get("footer"), max_length=300),
            box("Author line (small text on top)", c.get("author"), max_length=256)], done))

    @discord.ui.button(label="Send", emoji=I.e("check"), style=discord.ButtonStyle.success, row=1)
    async def send(self, interaction: discord.Interaction, _):
        if self.channel is None:
            return await interaction.response.send_message(err("Pick a channel first."), ephemeral=True)
        await interaction.response.defer()
        try:
            msg = await self.cog.send(self.author, self.channel, self.content, self.username, self.avatar,
                                      embed=await self.build_embed(), buttons=self.buttons)
        except VCError as e:
            return await interaction.followup.send(err(str(e)), ephemeral=True)
        self.last = (msg.channel.id, msg.id)
        await interaction.edit_original_response(embeds=await self.embeds(), view=self)
        await interaction.followup.send(ok(f"Sent in {self.channel.mention}: {msg.jump_url}"), ephemeral=True)

    @discord.ui.button(label="Name", emoji=I.e("member"), style=discord.ButtonStyle.secondary, row=2)
    async def set_name(self, interaction: discord.Interaction, _):
        async def done(i: discord.Interaction, v: list):
            self.username = v[0][:80]
            self.save()
            await self.refresh(i)
        await interaction.response.send_modal(FormModal("Webhook name", [
            box("Name (empty = webhook default)", self.username, max_length=80)], done))

    async def pick_image(self, interaction: discord.Interaction, attr: str, title: str, label: str):
        async def done(i: discord.Interaction, item):
            await media.forget(self.cog.bot, getattr(self, attr))
            setattr(self, attr, item)
            self.save()
            await i.followup.send(ok(f"{title} updated." if item else f"{title} removed."), ephemeral=True)
            await self.refresh_message()
        await interaction.response.send_modal(media.ImageModal(
            self.cog.bot, self.guild, title, done, label=label, allow_clear=True, fallback=interaction.channel))

    @discord.ui.button(label="Avatar", emoji=I.e("upload"), style=discord.ButtonStyle.secondary, row=2)
    async def set_avatar(self, interaction: discord.Interaction, _):
        await self.pick_image(interaction, "avatar", "Webhook avatar", "Webhook avatar")

    @discord.ui.button(label="Embed image", emoji=I.e("image"), style=discord.ButtonStyle.secondary, row=2)
    async def set_image(self, interaction: discord.Interaction, _):
        await self.pick_image(interaction, "image", "Embed image", "Webhook embed image")

    @discord.ui.button(label="Thumbnail", emoji=I.e("image"), style=discord.ButtonStyle.secondary, row=2)
    async def set_thumb(self, interaction: discord.Interaction, _):
        await self.pick_image(interaction, "thumb", "Embed thumbnail", "Webhook embed thumbnail")

    @discord.ui.button(label="Add Button", emoji=I.e("plus"), style=discord.ButtonStyle.secondary, row=3)
    async def add_button(self, interaction: discord.Interaction, _):
        if len(self.buttons) >= MAX_BUTTONS:
            return await interaction.response.send_message(err(f"Max {MAX_BUTTONS} buttons. Remove one first."), ephemeral=True)

        async def done(i: discord.Interaction, v: list):
            label, url, emoji_text = v[0][:80], clean_url(v[1]), v[2]
            emoji = await emojis.normalize(self.cog.bot, self.guild, emoji_text)
            if not label and not emoji:
                raise VCError("Give the button a text or an emoji.")
            self.buttons.append({"label": label, "url": url, "emoji": emoji})
            self.save()
            await self.refresh(i)
        await interaction.response.send_modal(FormModal("Add a link button", [
            box("Button text", "", max_length=80, placeholder="Join server"),
            box("Link (https://...)", "", max_length=512, required=True),
            box("Emoji or emoji ID (optional)", "", max_length=64, placeholder="123456789012345678 or :name:")], done))

    @discord.ui.button(label="Remove Button", emoji=I.e("minus"), style=discord.ButtonStyle.secondary, row=3)
    async def remove_button(self, interaction: discord.Interaction, _):
        if not self.buttons:
            return await interaction.response.send_message(err("There are no buttons."), ephemeral=True)

        async def done(i: discord.Interaction, v: list):
            text = v[0].strip().lower()
            if text in ("all", "*"):
                self.buttons.clear()
            else:
                try:
                    n = int(text)
                except ValueError:
                    raise VCError("Send the button number (see the list above) or `all`.")
                if not 1 <= n <= len(self.buttons):
                    raise VCError(f"Pick a number from 1 to {len(self.buttons)}.")
                self.buttons.pop(n - 1)
            self.save()
            await self.refresh(i)
        await interaction.response.send_modal(FormModal("Remove a button", [
            box("Button number (or `all`)", "", max_length=5, required=True)], done))

    @discord.ui.button(label="Edit sent message", emoji=I.e("refresh"), style=discord.ButtonStyle.secondary, row=3)
    async def edit_sent(self, interaction: discord.Interaction, _):
        async def done(i: discord.Interaction, v: list):
            emb = await self.build_embed()
            if not v[1] and emb is None:
                raise VCError("Write the new text or build an embed.")
            ch, mid = self.cog.parse_target(self.guild, v[0], self.last, self.channel)
            await i.response.defer(ephemeral=True)
            await self.cog.edit(self.author, ch, mid, v[1], embed=emb, buttons=self.buttons)
            if not v[0] or self.last == (ch.id, mid):
                self.content = v[1]
            await i.followup.send(ok(f"Edited: https://discord.com/channels/{self.guild.id}/{ch.id}/{mid}"), ephemeral=True)
            await self.refresh_message()
        await interaction.response.send_modal(FormModal("Edit a webhook message", [
            box("Message link or ID (empty = last sent)", "", max_length=200),
            box("New text (the embed above is used too)", self.content, long=True, max_length=2000)], done))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=3)
    async def close(self, interaction: discord.Interaction, _):
        self.stop()
        await interaction.response.edit_message(view=None)


class Webhook(commands.Cog):
    """$webhook (send + edit through a webhook) and $ei (animated emoji IDs)."""

    def __init__(self, bot):
        self.bot = bot
        self._hooks: dict[int, discord.Webhook] = {}

    # ------------------------------------------------------------ helpers
    async def webhook_for(self, channel: discord.TextChannel) -> discord.Webhook:
        """Kon's own webhook of that channel (created on first use)."""
        wh = self._hooks.get(channel.id)
        if wh is not None:
            return wh
        if not channel.permissions_for(channel.guild.me).manage_webhooks:
            raise VCError(f"I need **Manage Webhooks** in {channel.mention}.")
        try:
            for w in await channel.webhooks():
                if w.user is not None and w.user.id == self.bot.user.id and w.token:
                    wh = w
                    break
            else:
                wh = await channel.create_webhook(name="Kon", reason="Kon $webhook")
        except discord.HTTPException as e:
            raise VCError(f"Discord refused to create the webhook ({e.status}).")
        self._hooks[channel.id] = wh
        return wh

    @staticmethod
    def check_member(member: discord.Member, channel: discord.abc.GuildChannel):
        p = channel.permissions_for(member)
        if not (p.view_channel and p.send_messages):
            raise VCError(f"You can't send messages in {channel.mention}.")

    @staticmethod
    def mentions(member: discord.Member) -> discord.AllowedMentions:
        allow = member.guild_permissions.mention_everyone          # same rule as a normal message
        return discord.AllowedMentions(everyone=allow, roles=allow, users=True)

    async def avatar_url(self, guild: discord.Guild, avatar) -> Optional[str]:
        if isinstance(avatar, dict):                       # uploaded / saved item
            return await media.fresh(self.bot, avatar)
        avatar = (avatar or "").strip()
        if not avatar:
            return None
        host = self.bot.get_cog("ImageHost")
        if host is not None:
            return await host.resolve_image(guild, avatar)
        if avatar.startswith(("http://", "https://")):
            return avatar
        raise VCError("Give an avatar link starting with `https://`.")

    async def prepare(self, guild: discord.Guild, content: str, allow_empty: bool = False) -> str:
        content = (content or "").strip()
        if not content:
            if allow_empty:
                return ""
            raise VCError("Write a message or build an embed first.")
        text, _ = await emojis.resolve_text(self.bot, content, guild)
        if len(text) > 2000:
            raise VCError(f"With the emoji codes filled in the message is **{len(text)}** characters (max 2000). Shorten it.")
        return text

    async def send(self, member: discord.Member, channel: discord.TextChannel, content: str,
                   username: str = "", avatar=None, embed: Optional[discord.Embed] = None,
                   buttons: Optional[list] = None) -> discord.WebhookMessage:
        self.check_member(member, channel)
        text = await self.prepare(channel.guild, content, allow_empty=embed is not None)
        avatar_url = await self.avatar_url(channel.guild, avatar)
        M = discord.utils.MISSING
        use_emoji = True
        for attempt in (1, 2, 3):
            wh = await self.webhook_for(channel)
            view = make_view(buttons or [], use_emoji)
            try:
                return await wh.send(text or M, username=username or M, avatar_url=avatar_url or M,
                                     embed=embed or M, view=view if view is not None else M, wait=True, allowed_mentions=self.mentions(member))
            except discord.NotFound:                                # webhook was deleted -> make a new one
                self._hooks.pop(channel.id, None)
                if attempt == 3:
                    raise VCError("The webhook disappeared. Try again.")
            except discord.HTTPException as e:
                if e.code == 50035 and buttons and use_emoji:       # a button emoji Discord refuses -> send without emojis
                    use_emoji = False
                    continue
                raise VCError(f"Discord refused the message ({e.status}).")
        raise VCError("Couldn't send the message. Try again.")

    async def edit(self, member: discord.Member, channel: discord.TextChannel, message_id: int, content: str,
                   embed: Optional[discord.Embed] = None, buttons=KEEP):
        self.check_member(member, channel)
        text = await self.prepare(channel.guild, content, allow_empty=embed is not None)
        wh = await self.webhook_for(channel)
        M = discord.utils.MISSING
        use_emoji = True
        for _ in (1, 2):
            view = M if buttons is KEEP else make_view(buttons or [], use_emoji)       # None = remove the buttons
            try:
                return await wh.edit_message(message_id, content=text or None,
                                             embeds=[embed] if embed is not None else M,
                                             view=view, allowed_mentions=self.mentions(member))
            except discord.NotFound:
                raise VCError("I can't find that message - it must be one my webhook sent in that channel.")
            except discord.HTTPException as e:
                if e.code == 50035 and buttons is not KEEP and buttons and use_emoji:
                    use_emoji = False
                    continue
                raise VCError("I can only edit messages that Kon's own webhook sent.")

    @staticmethod
    def parse_target(guild: discord.Guild, text: str, last: Optional[tuple], channel) -> tuple:
        """message link / ID / empty (= last sent) -> (channel, message id)."""
        text = (text or "").strip()
        m = MSG_LINK.search(text)
        if m:
            if int(m.group(1)) != guild.id:
                raise VCError("That message is from another server.")
            ch, mid = guild.get_channel(int(m.group(2))), int(m.group(3))
        elif text.isdigit():
            ch, mid = channel, int(text)
        elif not text and last:
            ch, mid = guild.get_channel(last[0]), last[1]
        else:
            raise VCError("Give a message link, a message ID, or leave it empty to edit the last one you sent.")
        if not isinstance(ch, discord.TextChannel):
            raise VCError("Pick the channel in the panel first (or use a full message link).")
        return ch, mid

    # ------------------------------------------------------------ commands
    @commands.hybrid_command(name="webhook", aliases=["wh"],
                             description="Send a message through a webhook (animated emojis work without Nitro)")
    @app_commands.describe(channel="Where to send it", text="Message - paste emoji IDs for animated emojis")
    @commands.guild_only()
    @can_webhook()
    async def webhook(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *, text: str = ""):
        """$webhook = editor · $webhook #channel text = send now"""
        if channel is not None and text.strip():
            msg = await self.send(ctx.author, channel, text)
            if ctx.interaction is None and ctx.channel.permissions_for(ctx.guild.me).manage_messages:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            return await ctx.send(ok(f"Sent in {channel.mention}: {msg.jump_url}\nEdit it with "
                                     f"`{PREFIX}webhookedit {msg.jump_url} new text`"),
                                  delete_after=20 if ctx.interaction is None else None)
        start = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        view = WebhookPanel(self, ctx, start)
        view.content = text.strip()
        view.message = await ctx.reply(embeds=await view.embeds(), view=view, mention_author=False)

    @commands.hybrid_command(name="webhookedit", aliases=["whe"], description="Edit a message Kon's webhook sent")
    @app_commands.describe(link="Message link", text="New text (emoji IDs work)")
    @commands.guild_only()
    @can_webhook()
    async def webhookedit(self, ctx: commands.Context, link: str, *, text: str):
        ch, mid = self.parse_target(ctx.guild, link, None, ctx.channel)
        await self.edit(ctx.author, ch, mid, text)
        await ctx.reply(ok(f"Edited: https://discord.com/channels/{ctx.guild.id}/{ch.id}/{mid}"), mention_author=False)

    # ------------------------------------------------------------ $ei
    @commands.hybrid_command(name="ei", aliases=["emojiids", "animated"],
                             description="List animated emojis with their IDs (or look up one emoji ID)")
    @app_commands.describe(search="Part of a name, `here` (this server only) or an emoji ID")
    @commands.guild_only()
    async def ei(self, ctx: commands.Context, *, search: str = ""):
        search = search.strip()
        if search.isdigit():                                       # $ei <id> -> what is it?
            hit = await emojis.find_emoji(self.bot, int(search))
            if hit is None:
                raise VCError("I can't find an emoji with that ID.")
            name, animated = hit
            e = discord.Embed(color=BLURPLE, title=f"{I.s('sparkles')} {name}",
                              description=f"{emojis.code(name, animated, int(search))}\n"
                                          f"**{'Animated' if animated else 'Static'}** · ID `{search}`")
            e.set_footer(text=f"Paste the ID in {PREFIX}webhook text - it turns into this emoji")
            return await ctx.reply(embed=e, mention_author=False)

        here_only = search.lower() == "here"
        needle = "" if here_only else search.lower()
        groups = []
        guilds = [ctx.guild] if here_only else [ctx.guild, *[g for g in self.bot.guilds
                                                          if g.id != ctx.guild.id and g.get_member(ctx.author.id)]]
        for g in guilds:
            ems = sorted((x for x in g.emojis if x.animated and needle in x.name.lower()), key=lambda x: x.name.lower())
            if ems:
                groups.append((g.name, ems))
        if not here_only:
            own = sorted((x for x in await emojis.app_emojis(self.bot) if x.animated and needle in x.name.lower()),
                         key=lambda x: x.name.lower())
            if own:
                groups.append(("Kon's own emojis", own))
        total = sum(len(ems) for _, ems in groups)
        if not total:
            raise VCError("No animated emojis found." + ("" if here_only else " (Only servers you and Kon share are listed.)"))

        pages_txt, cur, n = [], [], 0
        for title, ems in groups:
            new_group = True
            for em in ems:
                if new_group:
                    cur.append(f"**{title}**")
                    new_group = False
                cur.append(f"{em} `{em.id}` · {em.name}")
                n += 1
                if n == PER_PAGE:
                    pages_txt.append("\n".join(cur))
                    cur, n, new_group = [], 0, True
        if cur:
            pages_txt.append("\n".join(cur))
        pages = []
        for i, body in enumerate(pages_txt, 1):
            e = discord.Embed(color=BLURPLE, title=f"{I.s('sparkles')} Animated emojis ({total})", description=body)
            e.set_footer(text=f"Page {i}/{len(pages_txt)} · paste an ID in {PREFIX}webhook text - works without Nitro")
            pages.append(e)
        if len(pages) == 1:
            return await ctx.reply(embed=pages[0], mention_author=False)
        view = PageView(ctx.author.id, pages)
        view.message = await ctx.reply(embed=pages[0], view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(Webhook(bot))
