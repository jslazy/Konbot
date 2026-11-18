"""$webhook - send messages through a webhook into any channel, edit them later. Animated emojis work WITHOUT Nitro.

(needs the Manage Webhooks permission - or the bot owner / a $konextra owner)
$webhook                       open the editor: pick the channel, write the message, set a name + avatar, Send, Edit
$webhook #channel text...      send straight away (multi-line text works)
$webhookedit <message link> new text      edit a message Kon's webhook sent   (alias $whe)

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


class WebhookPanel(OwnedView):
    def __init__(self, cog: "Webhook", ctx: commands.Context, channel: Optional[discord.TextChannel]):
        super().__init__(ctx.author.id, timeout=900)
        self.cog, self.guild, self.author = cog, ctx.guild, ctx.author
        self.channel = channel
        self.content = ""
        self.username = ""
        self.avatar = ""                       # link or the name of an image saved with $tolink
        self.last: Optional[tuple] = None      # (channel id, message id) of the last message sent

    async def embed(self) -> discord.Embed:
        preview = "*empty - press **Message***"
        if self.content:
            preview, _ = await emojis.resolve_text(self.cog.bot, self.content, self.guild)
            preview = preview[:3900]
        e = discord.Embed(title=f"{I.s('link')} Webhook sender", color=BLURPLE, description=preview)
        e.add_field(name="Channel", value=self.channel.mention if self.channel else "*pick one below*")
        e.add_field(name="Name", value=self.username or "*webhook default*")
        e.add_field(name="Avatar", value=f"`{self.avatar[:40]}`" if self.avatar else "*default*")
        if self.last:
            e.add_field(name="Last sent", value=f"https://discord.com/channels/{self.guild.id}/{self.last[0]}/{self.last[1]}",
                        inline=False)
        e.set_footer(text="Paste an emoji ID or :name: - animated emojis work without Nitro")
        return e

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=await self.embed(), view=self)

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
            box("Message (emoji ID or :name: works)", self.content, long=True, max_length=2000, required=True)], done))

    @discord.ui.button(label="Name & avatar", emoji=I.e("member"), style=discord.ButtonStyle.secondary, row=1)
    async def set_identity(self, interaction: discord.Interaction, _):
        async def done(i: discord.Interaction, v: list):
            self.username, self.avatar = v[0][:80], v[1]
            if self.avatar:                                    # fail now, not on Send
                await self.cog.avatar_url(self.guild, self.avatar)
            await self.refresh(i)
        await interaction.response.send_modal(FormModal("Webhook name & avatar", [
            box("Name (empty = webhook default)", self.username, max_length=80),
            box("Avatar: link or saved image name", self.avatar, max_length=300,
                placeholder="https://...  or  a name from $links")], done))

    @discord.ui.button(label="Send", emoji=I.e("check"), style=discord.ButtonStyle.success, row=1)
    async def send(self, interaction: discord.Interaction, _):
        if self.channel is None:
            return await interaction.response.send_message(err("Pick a channel first."), ephemeral=True)
        await interaction.response.defer()
        try:
            msg = await self.cog.send(self.author, self.channel, self.content, self.username, self.avatar)
        except VCError as e:
            return await interaction.followup.send(err(str(e)), ephemeral=True)
        self.last = (msg.channel.id, msg.id)
        await interaction.edit_original_response(embed=await self.embed(), view=self)
        await interaction.followup.send(ok(f"Sent in {self.channel.mention}: {msg.jump_url}"), ephemeral=True)

    @discord.ui.button(label="Edit sent message", emoji=I.e("refresh"), style=discord.ButtonStyle.secondary, row=2)
    async def edit_sent(self, interaction: discord.Interaction, _):
        async def done(i: discord.Interaction, v: list):
            if not v[1]:
                raise VCError("Write the new text.")
            ch, mid = self.cog.parse_target(self.guild, v[0], self.last, self.channel)
            await i.response.defer(ephemeral=True)
            await self.cog.edit(self.author, ch, mid, v[1])
            if not v[0] or self.last == (ch.id, mid):
                self.content = v[1]
            await i.followup.send(ok(f"Edited: https://discord.com/channels/{self.guild.id}/{ch.id}/{mid}"), ephemeral=True)
            if self.message:
                try:
                    await self.message.edit(embed=await self.embed(), view=self)
                except discord.HTTPException:
                    pass
        await interaction.response.send_modal(FormModal("Edit a webhook message", [
            box("Message link or ID (empty = last sent)", "", max_length=200),
            box("New text", self.content, long=True, max_length=2000, required=True)], done))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=2)
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

    async def avatar_url(self, guild: discord.Guild, avatar: str) -> Optional[str]:
        avatar = (avatar or "").strip()
        if not avatar:
            return None
        host = self.bot.get_cog("ImageHost")
        if host is not None:
            return await host.resolve_image(guild, avatar)
        if avatar.startswith(("http://", "https://")):
            return avatar
        raise VCError("Give an avatar link starting with `https://`.")

    async def prepare(self, guild: discord.Guild, content: str) -> str:
        content = (content or "").strip()
        if not content:
            raise VCError("Write a message first.")
        text, _ = await emojis.resolve_text(self.bot, content, guild)
        if len(text) > 2000:
            raise VCError(f"With the emoji codes filled in the message is **{len(text)}** characters (max 2000). Shorten it.")
        return text

    async def send(self, member: discord.Member, channel: discord.TextChannel, content: str,
                   username: str = "", avatar: str = "") -> discord.WebhookMessage:
        self.check_member(member, channel)
        text = await self.prepare(channel.guild, content)
        avatar_url = await self.avatar_url(channel.guild, avatar)
        for attempt in (1, 2):
            wh = await self.webhook_for(channel)
            try:
                return await wh.send(text, username=username or discord.utils.MISSING,
                                     avatar_url=avatar_url or discord.utils.MISSING, wait=True,
                                     allowed_mentions=self.mentions(member))
            except discord.NotFound:                                # webhook was deleted -> make a new one
                self._hooks.pop(channel.id, None)
                if attempt == 2:
                    raise VCError("The webhook disappeared. Try again.")
            except discord.HTTPException as e:
                raise VCError(f"Discord refused the message ({e.status}).")

    async def edit(self, member: discord.Member, channel: discord.TextChannel, message_id: int, content: str):
        self.check_member(member, channel)
        text = await self.prepare(channel.guild, content)
        wh = await self.webhook_for(channel)
        try:
            return await wh.edit_message(message_id, content=text, allowed_mentions=self.mentions(member))
        except discord.NotFound:
            raise VCError("I can't find that message - it must be one my webhook sent in that channel.")
        except discord.HTTPException:
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
        view.message = await ctx.reply(embed=await view.embed(), view=view, mention_author=False)

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
