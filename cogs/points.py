"""Point system: chat + voice points, $profile, leaderboard, $point config, badges."""
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import FieldsModal, OwnedView, parse_int

DEFAULTS = {
    "chat_enabled": True, "chat_msgs": 50, "chat_points": 1, "chat_everywhere": True,
    "vc_enabled": True, "vc_mins": 5, "vc_points": 1, "vc_need_others": True,
    "vc_need_muted": True,      # voice points ONLY while the member is muted / deafened (mic off)
    "cooldown": 5,
    "channel_rules": {},        # "<channel_id>": {"msgs": 50, "points": 1}
    "disabled_channels": [],    # chat points are NOT counted here (channel or whole category)
    "enabled_channels": [],     # used when "Everywhere" is OFF: chat points ARE counted here (default rule)
    "vc_disabled_channels": [], # voice points are NOT counted here (voice channel or whole category)
}
LIST_KEYS = ("disabled_channels", "enabled_channels", "vc_disabled_channels")
TICK = 30
TEXT_TYPES = [discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum,
              discord.ChannelType.voice, discord.ChannelType.stage_voice, discord.ChannelType.category]
VOICE_TYPES = [discord.ChannelType.voice, discord.ChannelType.stage_voice, discord.ChannelType.category]
MAX_SHOWN = 25


BADGE_MAX = 100          # a custom emoji alone is ~30 characters, so the old limit of 30 cut it in half
_SHORTCODE = re.compile(r"(?<![<\w]):(\w{2,32}):(?!\d)")      # :name: (but not the inside of <:name:id>)


def clean_badge(bot, raw: str) -> str:
    """Badge text exactly as typed: custom emojis stay whole (<:name:id> / <a:name:id>), :name: turns into the emoji
    when the bot can see it, and nothing is ever cut in the middle of an emoji."""
    name = " ".join((raw or "").split())

    def sub(m):
        e = discord.utils.get(bot.emojis, name=m.group(1))
        return str(e) if e else m.group(0)
    name = _SHORTCODE.sub(sub, name)
    if not name:
        raise VCError("Give the badge a name or an emoji.")
    if len(name) > BADGE_MAX:
        raise VCError(f"That badge is too long (max **{BADGE_MAX}** characters).")
    return name


def show_badge(name: str) -> str:
    return name if "<" in name else f"**{name}**"      # emojis render best without bold markers


def onoff(v: bool) -> str:
    return "ON" if v else "OFF"


def chan_ids(ch) -> set:
    """ids that can switch a channel off/on: the channel, its thread parent and its category."""
    ids = {ch.id}
    parent = getattr(ch, "parent", None)
    for c in (ch, parent):
        if c is None:
            continue
        if getattr(c, "id", None):
            ids.add(c.id)
        if getattr(c, "category_id", None):
            ids.add(c.category_id)
    if getattr(ch, "parent_id", None):
        ids.add(ch.parent_id)
    return ids


def fmt_channels(guild: discord.Guild, ids: list, limit: int = 15) -> str:
    if not ids:
        return "*none*"
    out = []
    for cid in ids[:limit]:
        ch = guild.get_channel(cid)
        if ch is None:
            out.append(f"~~deleted `{cid}`~~")
        elif isinstance(ch, discord.CategoryChannel):
            out.append(f"**{ch.name}** (category)")
        else:
            out.append(ch.mention)
    more = f" +{len(ids) - limit} more" if len(ids) > limit else ""
    return " · ".join(out) + more


class PointConfigView(OwnedView):
    """Two pages: Main (rules + switches) and Channels (where points count / don't count)."""

    def __init__(self, cog: "Points", invoker: discord.Member):
        super().__init__(invoker.id, timeout=600)
        self.cog, self.guild = cog, invoker.guild
        self.page = "main"
        self.render()

    @property
    def cfg(self):
        return self.cog.cfg(self.guild.id)

    # ----- embeds -----
    def embed(self) -> discord.Embed:
        return self.embed_main() if self.page == "main" else self.embed_channels()

    def embed_main(self) -> discord.Embed:
        c = self.cfg
        off = len(c["disabled_channels"]) + len(c["vc_disabled_channels"])
        return discord.Embed(title=f"{I.s('settings')} Point Configuration", color=BRAND, description=(
            f"{I.s('message')} **Chat {onoff(c['chat_enabled'])}** · {c['chat_msgs']} msgs = {c['chat_points']} pt · "
            f"cooldown {c['cooldown']}s · everywhere {onoff(c['chat_everywhere'])}\n"
            f"{I.s('mic')} **Voice {onoff(c['vc_enabled'])}** · {c['vc_mins']} min = {c['vc_points']} pt · "
            f"needs 2+ people {onoff(c['vc_need_others'])} · only when mic off/deaf {onoff(c['vc_need_muted'])}\n"
            f"{I.s('lock')} Points off in **{off}** channel(s) · custom rules **{len(c['channel_rules'])}**"))

    def embed_channels(self) -> discord.Embed:
        c, g = self.cfg, self.guild
        everywhere = c["chat_everywhere"]
        e = discord.Embed(title=f"{I.s('lock')} Point Channels", color=BLURPLE, description=(
            f"**Mode:** " + ("Count **everywhere** except the disabled channels" if everywhere
                             else "Count **only** in the enabled channels + channels with a rule")
            + "\nA category covers every channel inside it."))
        e.add_field(name=f"{I.s('cross')} Chat points OFF in", inline=False, value=fmt_channels(g, c["disabled_channels"]))
        if not everywhere:
            e.add_field(name=f"{I.s('check')} Chat points ON in", inline=False, value=fmt_channels(g, c["enabled_channels"]))
        e.add_field(name=f"{I.s('mute')} Voice points OFF in", inline=False, value=fmt_channels(g, c["vc_disabled_channels"]))
        rules = [f"<#{cid}> · **{r['msgs']}** msgs = **{r['points']}** pt" for cid, r in c["channel_rules"].items()]
        e.add_field(name=f"{I.s('tag')} Custom rules", inline=False,
                    value="\n".join(rules[:10]) + (f"\n+{len(rules) - 10} more" if len(rules) > 10 else "") if rules else "*none*")
        e.set_footer(text="Un-select a channel to turn its points back on.")
        return e

    # ----- components -----
    def _btn(self, label, emoji, cb, row, style=discord.ButtonStyle.secondary):
        b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, row=row)
        b.callback = cb
        self.add_item(b)

    def _select(self, key: str, placeholder: str, types, row: int):
        ids = self.cfg[key][:MAX_SHOWN]
        shown = [ch for ch in (self.guild.get_channel(i) for i in ids) if ch is not None]
        sel = discord.ui.ChannelSelect(placeholder=placeholder, channel_types=types, min_values=0,
                                       max_values=MAX_SHOWN, default_values=shown, row=row)

        async def picked(i: discord.Interaction):
            old = self.cfg[key]
            shown_ids = {ch.id for ch in shown}
            kept = [x for x in old if x not in shown_ids]            # channels that weren't in the menu stay
            self.cfg[key] = kept + [ch.id for ch in sel.values]
            if key == "disabled_channels":                              # a channel can't be both
                self.cfg["enabled_channels"] = [x for x in self.cfg["enabled_channels"] if x not in self.cfg[key]]
            elif key == "enabled_channels":
                self.cfg["disabled_channels"] = [x for x in self.cfg["disabled_channels"] if x not in self.cfg[key]]
            self.save()
            await self.push(i)
        sel.callback = picked
        self.add_item(sel)

    def render(self):
        self.clear_items()
        c = self.cfg
        if self.page == "main":
            self._btn("Chat Rule", "message", self.chat_rule, 0)
            self._btn("VC Rule", "mic", self.vc_rule, 0)
            self._btn("Cooldown", "clock", self.cooldown, 0)
            self._btn(f"Chat: {onoff(c['chat_enabled'])}", "message", self.t_chat, 1,
                      discord.ButtonStyle.success if c["chat_enabled"] else discord.ButtonStyle.danger)
            self._btn(f"VC: {onoff(c['vc_enabled'])}", "mic", self.t_vc, 1,
                      discord.ButtonStyle.success if c["vc_enabled"] else discord.ButtonStyle.danger)
            self._btn(f"VC needs others: {onoff(c['vc_need_others'])}", "users", self.t_others, 2)
            self._btn(f"Only mic off/deaf: {onoff(c['vc_need_muted'])}", "mute", self.t_muted, 2)
            self._btn("Channels", "lock", self.go_channels, 3, discord.ButtonStyle.primary)
            self._btn("Close", "cross", self.close, 3)
        else:
            self._select("disabled_channels", "Chat points OFF in... (pick channels / categories)", TEXT_TYPES, 0)
            row = 1
            if not c["chat_everywhere"]:
                self._select("enabled_channels", "Chat points ON in... (pick channels / categories)", TEXT_TYPES, row)
                row += 1
            self._select("vc_disabled_channels", "Voice points OFF in... (voice channels / categories)", VOICE_TYPES, row)
            self._btn("Mode: everywhere" if c["chat_everywhere"] else "Mode: only enabled", "link", self.t_everywhere, 3,
                      discord.ButtonStyle.success if c["chat_everywhere"] else discord.ButtonStyle.primary)
            self._btn("Add Rule", "plus", self.add_rule, 3)
            self._btn("Remove Rule", "minus", self.remove_rule, 3)
            self._btn("Back", "left", self.go_main, 3)

    async def push(self, interaction: discord.Interaction):
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def refresh_message(self):
        if self.message:
            self.render()
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass

    def save(self):
        self.cog.save_cfg(self.guild.id, self.cfg)

    async def go_channels(self, i):
        self.page = "channels"
        await self.push(i)

    async def go_main(self, i):
        self.page = "main"
        await self.push(i)

    # ----- modals -----
    async def chat_rule(self, interaction):
        c = self.cfg

        async def cb(i, v):
            c["chat_msgs"] = parse_int(v[0], 1, 100000, "Messages")
            c["chat_points"] = parse_int(v[1], 1, 1000, "Points")
            self.save()
            await self.push(i)
        await interaction.response.send_modal(FieldsModal("Chat Rule", [
            ("Messages needed", str(c["chat_msgs"]), 6, True), ("Points awarded", str(c["chat_points"]), 4, True)], cb))

    async def vc_rule(self, interaction):
        c = self.cfg

        async def cb(i, v):
            c["vc_mins"] = parse_int(v[0], 1, 10000, "Minutes")
            c["vc_points"] = parse_int(v[1], 1, 1000, "Points")
            self.save()
            await self.push(i)
        await interaction.response.send_modal(FieldsModal("Voice Rule", [
            ("Minutes in VC needed", str(c["vc_mins"]), 5, True), ("Points awarded", str(c["vc_points"]), 4, True)], cb))

    async def cooldown(self, interaction):
        c = self.cfg

        async def cb(i, v):
            c["cooldown"] = parse_int(v[0], 0, 600, "Cooldown")
            self.save()
            await self.push(i)
        await interaction.response.send_modal(FieldsModal("Message Cooldown", [
            ("Seconds between counted messages", str(c["cooldown"]), 3, True)], cb))

    # ----- channel rules -----
    async def add_rule(self, interaction):
        view = discord.ui.View(timeout=120)
        sel = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum])

        async def picked(i: discord.Interaction):
            ch = sel.values[0]
            cid = str(ch.id)

            async def cb(i2, v):
                self.cfg["channel_rules"][cid] = {"msgs": parse_int(v[0], 1, 100000, "Messages"),
                                                  "points": parse_int(v[1], 1, 1000, "Points")}
                self.save()
                await i2.response.send_message(ok(f"Rule saved for <#{cid}>."), ephemeral=True)
                await self.refresh_message()
            cur = self.cfg["channel_rules"].get(cid, {"msgs": self.cfg["chat_msgs"], "points": self.cfg["chat_points"]})
            await i.response.send_modal(FieldsModal(f"Rule: #{ch.name}"[:45], [
                ("Messages needed", str(cur["msgs"]), 6, True), ("Points awarded", str(cur["points"]), 4, True)], cb))
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Pick a channel:", view=view, ephemeral=True)

    async def remove_rule(self, interaction):
        rules = self.cfg["channel_rules"]
        if not rules:
            return await interaction.response.send_message(err("No channel rules yet."), ephemeral=True)
        opts = []
        for cid, r in list(rules.items())[:25]:
            ch = self.guild.get_channel(int(cid))
            opts.append(discord.SelectOption(label=f"#{ch.name}" if ch else f"deleted channel {cid}", value=cid,
                                             description=f"{r['msgs']} msgs = {r['points']} pt(s)"))
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder="Select a rule to remove...", options=opts)

        async def picked(i: discord.Interaction):
            self.cfg["channel_rules"].pop(sel.values[0], None)
            self.save()
            await i.response.edit_message(content=ok("Rule removed."), view=None)
            await self.refresh_message()
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Remove which rule?", view=view, ephemeral=True)

    # ----- toggles -----
    async def t_chat(self, i):
        self.cfg["chat_enabled"] = not self.cfg["chat_enabled"]; self.save(); await self.push(i)

    async def t_vc(self, i):
        self.cfg["vc_enabled"] = not self.cfg["vc_enabled"]; self.save(); await self.push(i)

    async def t_everywhere(self, i):
        self.cfg["chat_everywhere"] = not self.cfg["chat_everywhere"]; self.save(); await self.push(i)

    async def t_others(self, i):
        self.cfg["vc_need_others"] = not self.cfg["vc_need_others"]; self.save(); await self.push(i)

    async def t_muted(self, i):
        self.cfg["vc_need_muted"] = not self.cfg["vc_need_muted"]; self.save(); await self.push(i)

    async def close(self, interaction):
        self.stop()
        await interaction.response.edit_message(view=None)


class Points(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._cfg: dict[int, dict] = {}
        self._last: dict[tuple, float] = {}
        self._tick_at = time.monotonic()

    async def cog_load(self):
        fixed = self.db.repair_badges()           # badges saved half-cut by the old 30-character limit
        if fixed:
            import logging
            logging.getLogger("kon.points").info("repaired %d cut-off emoji badge(s)", fixed)
        self.vc_tick.start()

    async def cog_unload(self):
        self.vc_tick.cancel()

    # ---------- config ----------
    def cfg(self, gid: int) -> dict:
        c = self._cfg.get(gid)
        if c is None:
            stored = self.db.get_settings(gid, "points")
            c = {**DEFAULTS, **stored}
            c["channel_rules"] = dict(stored.get("channel_rules", {}))
            for k in LIST_KEYS:
                c[k] = [int(x) for x in stored.get(k, [])]
            self._cfg[gid] = c
        return c

    def save_cfg(self, gid: int, cfg: dict):
        self._cfg[gid] = cfg
        self.db.set_settings(gid, "points", cfg)

    # ---------- earning ----------
    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if m.author.bot or not m.guild or m.content.startswith(PREFIX):
            return
        if not m.content.strip() and not m.attachments:
            return
        if self.bot.is_noprefix(m.author):           # a no-prefix command must not count as chat
            ctx = await self.bot.get_context(m)
            if ctx.valid:
                return
        cfg = self.cfg(m.guild.id)
        if not cfg["chat_enabled"]:
            return
        cid = getattr(m.channel, "parent_id", None) or m.channel.id
        ids = chan_ids(m.channel)
        if ids & set(cfg["disabled_channels"]):          # points switched off here (channel or its category)
            return
        rule = cfg["channel_rules"].get(str(cid))
        if rule:
            per, pts, use_ch = rule["msgs"], rule["points"], True
        elif cfg["chat_everywhere"] or ids & set(cfg["enabled_channels"]):
            per, pts, use_ch = cfg["chat_msgs"], cfg["chat_points"], False
        else:
            return
        now = time.monotonic()
        key = (m.guild.id, m.author.id)
        if now - self._last.get(key, -1e9) < cfg["cooldown"]:
            return
        if len(self._last) > 50000:
            self._last.clear()
        self._last[key] = now
        self.db.bump_chat(m.guild.id, m.author.id, cid, per, pts, use_ch)

    @tasks.loop(seconds=TICK)
    async def vc_tick(self):
        now = time.monotonic()
        secs = int(min(max(now - self._tick_at, 1), 90))
        self._tick_at = now
        rows = []
        for guild in self.bot.guilds:
            cfg = self.cfg(guild.id)
            if not cfg["vc_enabled"]:
                continue
            afk = guild.afk_channel
            off = set(cfg["vc_disabled_channels"])
            for vc in guild.voice_channels:
                if afk and vc.id == afk.id:
                    continue
                if vc.id in off or (vc.category_id and vc.category_id in off):
                    continue
                humans = [m for m in vc.members if not m.bot]
                if cfg["vc_need_others"] and len(humans) < 2:
                    continue
                for m in humans:
                    vs = m.voice
                    if vs is None or vs.afk:
                        continue
                    if cfg["vc_need_muted"]:
                        # points only while the mic is off or the member is deafened (self or server)
                        if not (vs.self_mute or vs.mute or vs.self_deaf or vs.deaf):
                            continue
                    elif vs.deaf or vs.self_deaf:               # old rule (switch OFF): everyone except deafened
                        continue
                    rows.append((guild.id, m.id, secs, cfg["vc_mins"] * 60, cfg["vc_points"]))
        if rows:
            self.db.bump_vc_many(rows)

    @vc_tick.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()
        self._tick_at = time.monotonic()

    # ---------- profile ----------
    @commands.hybrid_command(name="profile", aliases=['stats', 'pf'], description="View your points, chat and VC progress")
    @app_commands.describe(member="Member to look at (default: you)")
    @commands.guild_only()
    async def profile(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        gid = ctx.guild.id
        u = self.db.user(gid, member.id)
        c = self.cfg(gid)
        badges = self.db.badges(gid, member.id)
        embed = discord.Embed(
            description=(
                f"{I.s('heart')} **Points:** {u['points']}\n"
                f"{I.s('message')} **Chat Progress:** {u['chat_progress']}/{c['chat_msgs']} msgs\n"
                f"{I.s('mic')} **VC Progress:** {u['vc_progress'] // 60}/{c['vc_mins']} mins\n\n"
                f"**Badges:** {', '.join(badges) if badges else 'None'}"),
            color=BLURPLE, timestamp=discord.utils.utcnow())
        embed.set_author(name=f"{member.name}'s Status",
                         icon_url=ctx.guild.icon.url if ctx.guild.icon else member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    # ---------- leaderboard ----------
    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top"], description="Top 10 members by points")
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context):
        rows = self.db.leaderboard(ctx.guild.id, 10)
        lines = [f"{i}. <@{r['user_id']}> — `{r['points']}` pts | `{r['chat_total']}` msgs"
                 for i, r in enumerate(rows, 1)]
        embed = discord.Embed(description="\n".join(lines) or "No points yet. Start chatting!",
                              color=BLURPLE, timestamp=discord.utils.utcnow())
        icon = ctx.guild.icon.url if ctx.guild.icon else self.bot.user.display_avatar.url
        embed.set_author(name="🏆 Top 10 Leaderboard", icon_url=icon)
        embed.set_thumbnail(url=icon)
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    # ---------- $point (admin) ----------
    @commands.group(name="point", hidden=True, invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def point(self, ctx: commands.Context):
        """Admin: config / add / remove / set / reset."""
        await ctx.reply(f"{I.s('settings')} `{PREFIX}point config` · `enable` · `disable` · `add` · `remove` · `set` · `reset`",
                        mention_author=False)

    @point.command(name="config")
    @commands.has_permissions(manage_guild=True)
    async def point_config(self, ctx: commands.Context):
        """Customize message / voice points, cooldown and channel rules."""
        view = PointConfigView(self, ctx.author)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)

    @point.command(name="disable", aliases=["off"])
    @commands.has_permissions(manage_guild=True)
    async def point_disable(self, ctx, channels: commands.Greedy[discord.abc.GuildChannel]):
        """$point disable [#channels / categories] - stop counting points there (empty = this channel)"""
        channels = channels or [ctx.channel]
        c = self.cfg(ctx.guild.id)
        for ch in channels:
            if ch.id not in c["disabled_channels"]:
                c["disabled_channels"].append(ch.id)
            if isinstance(ch, (discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel)) \
                    and ch.id not in c["vc_disabled_channels"]:
                c["vc_disabled_channels"].append(ch.id)
            c["enabled_channels"] = [x for x in c["enabled_channels"] if x != ch.id]
        self.save_cfg(ctx.guild.id, c)
        await ctx.reply(ok("Points off in " + ", ".join(ch.mention for ch in channels) + "."),
                        mention_author=False)

    @point.command(name="enable", aliases=["on"])
    @commands.has_permissions(manage_guild=True)
    async def point_enable(self, ctx, channels: commands.Greedy[discord.abc.GuildChannel]):
        """$point enable [#channels / categories] - count points there again (empty = this channel)"""
        channels = channels or [ctx.channel]
        c = self.cfg(ctx.guild.id)
        for ch in channels:
            c["disabled_channels"] = [x for x in c["disabled_channels"] if x != ch.id]
            c["vc_disabled_channels"] = [x for x in c["vc_disabled_channels"] if x != ch.id]
            if not c["chat_everywhere"] and ch.id not in c["enabled_channels"]:
                c["enabled_channels"].append(ch.id)
        self.save_cfg(ctx.guild.id, c)
        await ctx.reply(ok("Points on in " + ", ".join(ch.mention for ch in channels) + "."),
                        mention_author=False)

    @point.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def point_add(self, ctx, member: discord.Member, amount: commands.Range[int, 1, 10_000_000]):
        """$point add @user <amount>"""
        new = self.db.add_points(ctx.guild.id, member.id, amount, f"admin add by {ctx.author.id}")
        await ctx.reply(ok(f"Gave {member.mention} **{amount}** points. Now **{new}**."), mention_author=False)

    @point.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def point_remove(self, ctx, member: discord.Member, amount: commands.Range[int, 1, 10_000_000]):
        """$point remove @user <amount>"""
        new = self.db.add_points(ctx.guild.id, member.id, -amount, f"admin remove by {ctx.author.id}")
        await ctx.reply(ok(f"Took **{amount}** points from {member.mention}. Now **{new}**."), mention_author=False)

    @point.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def point_set(self, ctx, member: discord.Member, amount: commands.Range[int, 0, 100_000_000]):
        """$point set @user <amount>"""
        new = self.db.set_points(ctx.guild.id, member.id, amount)
        await ctx.reply(ok(f"{member.mention} has **{new}** points now."), mention_author=False)

    @point.command(name="reset")
    @commands.has_permissions(manage_guild=True)
    async def point_reset(self, ctx, member: discord.Member):
        """$point reset @user - wipe points, progress and badges."""
        self.db.reset_user(ctx.guild.id, member.id)
        await ctx.reply(ok(f"Reset {member.mention}."), mention_author=False)

    # ---------- badges ----------
    @commands.group(name="badge", invoke_without_command=True)
    @commands.guild_only()
    async def badge(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """$badge [@user] - list badges."""
        member = member or ctx.author
        b = self.db.badges(ctx.guild.id, member.id)
        await ctx.reply(f"{I.s('trophy')} **{member.display_name}** badges: {', '.join(b) if b else 'None'}",
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @badge.command(name="give")
    @commands.has_permissions(manage_guild=True)
    async def badge_give(self, ctx, member: discord.Member, *, name: str):
        """$badge give @user <name>"""
        name = clean_badge(self.bot, name)
        if not self.db.add_badge(ctx.guild.id, member.id, name):
            raise VCError("They already have it.")
        await ctx.reply(ok(f"Gave {show_badge(name)} to {member.mention}."), mention_author=False)

    @badge.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def badge_remove(self, ctx, member: discord.Member, *, name: str):
        """$badge remove @user <name>"""
        name = clean_badge(self.bot, name)
        if not self.db.remove_badge(ctx.guild.id, member.id, name):
            raise VCError("They don't have that badge.")
        await ctx.reply(ok(f"Removed {show_badge(name)} from {member.mention}."), mention_author=False)


async def setup(bot):
    await bot.add_cog(Points(bot))
