"""$aura - plain message: "Aura this user holds! @user:" + `[██████░░░░]` `60%` (the bar fills up slowly, message is edited).

    $aura                    your aura
    $aura @member            theirs (also works as a reply, or with an ID)
    $aura <any text>         your aura (extra text is ignored, e.g. "$aura of ultimate lazyness")
    $aura config             (admins) fix the aura of members / roles, turn random aura on / off

Priority: fixed aura of the MEMBER > fixed aura of their highest ROLE that has one > random aura (stable for the day).
"""
import asyncio
import datetime
import random
from typing import Optional

import discord
from discord.ext import commands

from core.config import BLURPLE, GREEN, TIMEZONE
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import FieldsModal, OwnedView, parse_int

KEY = "aura"
SEGMENTS = 10


def bar(p: int) -> str:
    """White blocks (filled) on dotted-black blocks (empty): inside the code chip `[........]` like the original message."""
    n = round(p / 100 * SEGMENTS)
    return "█" * n + "░" * (SEGMENTS - n)


def tier(p: int) -> str:
    return ("Legendary" if p >= 90 else "Strong" if p >= 70 else "Average" if p >= 40 else "Weak" if p >= 15 else "Drained")


class AuraConfigView(OwnedView):
    def __init__(self, cog: "Aura", invoker: discord.Member):
        super().__init__(invoker.id, timeout=600)
        self.cog, self.guild = cog, invoker.guild
        self.render()

    @property
    def cfg(self) -> dict:
        return self.cog.cfg(self.guild.id)

    def embed(self) -> discord.Embed:
        c = self.cfg
        mem = [f"<@{u}> {p}%" for u, p in c["members"].items()]
        rol = [f"<@&{r}> {p}%" for r, p in c["roles"].items()]
        more = lambda l: " · ".join(l[:12]) + (f" +{len(l) - 12} more" if len(l) > 12 else "") if l else "*none*"
        return discord.Embed(title=f"{I.s('fire')} Aura Configuration", color=GREEN, description=(
            f"Random aura: **{'ON' if c['random'] else 'OFF'}**\n"
            f"{I.s('user_star')} **Members:** {more(mem)}\n{I.s('crown')} **Roles:** {more(rol)}\n"
            f"-# Member beats role · highest role wins"))

    def render(self):
        self.clear_items()

        def btn(label, emoji, cb, row, style=discord.ButtonStyle.secondary):
            b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, row=row)
            b.callback = cb
            self.add_item(b)
        btn("Set Member", "user_star", self.set_member, 0, discord.ButtonStyle.primary)
        btn("Set Role", "crown", self.set_role, 0, discord.ButtonStyle.primary)
        btn("Remove", "minus", self.remove, 0)
        btn(f"Random: {'ON' if self.cfg['random'] else 'OFF'}", "refresh", self.t_random, 1,
            discord.ButtonStyle.success if self.cfg["random"] else discord.ButtonStyle.danger)
        btn("Close", "cross", self.close, 1)

    async def refresh(self):
        if self.message:
            self.render()
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass

    # ----- pick a member / role, then ask for the % -----
    async def _pick(self, interaction: discord.Interaction, kind: str):
        view = discord.ui.View(timeout=120)
        sel = discord.ui.UserSelect(placeholder="Select a member...") if kind == "members" \
            else discord.ui.RoleSelect(placeholder="Select a role...")

        async def picked(i: discord.Interaction):
            target = sel.values[0]
            if kind == "roles" and target.is_default():
                return await i.response.send_message(err("Pick a real role, not @everyone."), ephemeral=True)
            cur = self.cfg[kind].get(str(target.id), "")

            async def cb(i2, v):
                pct = parse_int(v[0], 0, 100, "Aura %")
                self.cfg[kind][str(target.id)] = pct
                self.cog.save(self.guild.id)
                await i2.response.send_message(ok(f"{target.mention} now has **{pct}%** aura."), ephemeral=True,
                                               allowed_mentions=discord.AllowedMentions.none())
                await self.refresh()
            name = getattr(target, "display_name", None) or target.name
            await i.response.send_modal(FieldsModal(f"Aura: {name}"[:45], [("Aura % (0 - 100)", str(cur), 3, True)], cb))
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message(
            "Pick the member:" if kind == "members" else "Pick the role:", view=view, ephemeral=True)

    async def set_member(self, interaction):
        await self._pick(interaction, "members")

    async def set_role(self, interaction):
        await self._pick(interaction, "roles")

    async def remove(self, interaction: discord.Interaction):
        c = self.cfg
        opts = []
        for u, p in list(c["members"].items())[:15]:
            m = self.guild.get_member(int(u))
            opts.append(discord.SelectOption(label=(m.display_name if m else f"member {u}")[:90], value=f"members:{u}",
                                             description=f"{p}%"))
        for r, p in list(c["roles"].items())[:10]:
            ro = self.guild.get_role(int(r))
            opts.append(discord.SelectOption(label=f"@{ro.name}"[:90] if ro else f"role {r}", value=f"roles:{r}",
                                             description=f"{p}%"))
        if not opts:
            return await interaction.response.send_message(err("Nothing is configured yet."), ephemeral=True)
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder="Select what to remove...", options=opts[:25])

        async def picked(i: discord.Interaction):
            kind, _, ident = sel.values[0].partition(":")
            c[kind].pop(ident, None)
            self.cog.save(self.guild.id)
            await i.response.edit_message(content=ok("Removed."), view=None)
            await self.refresh()
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Which one should be removed?", view=view, ephemeral=True)

    async def t_random(self, interaction: discord.Interaction):
        self.cfg["random"] = not self.cfg["random"]
        self.cog.save(self.guild.id)
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=None)


class Aura(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cfg: dict[int, dict] = {}

    # ---------- storage ----------
    def cfg(self, gid: int) -> dict:
        c = self._cfg.get(gid)
        if c is None:
            s = self.bot.db.get_settings(gid, KEY)
            c = self._cfg[gid] = {"members": dict(s.get("members", {})), "roles": dict(s.get("roles", {})),
                                  "random": s.get("random", True)}
        return c

    def save(self, gid: int):
        self.bot.db.set_settings(gid, KEY, self.cfg(gid))

    def value(self, member: discord.Member) -> tuple[Optional[int], str]:
        c = self.cfg(member.guild.id)
        v = c["members"].get(str(member.id))
        if v is not None:
            return int(v), "set for this member by an admin"
        for r in sorted(member.roles, key=lambda r: r.position, reverse=True):
            if str(r.id) in c["roles"]:
                return int(c["roles"][str(r.id)]), f"set for the role {r.name}"
        if c["random"]:
            day = datetime.datetime.now(TIMEZONE).date().isoformat()
            return random.Random(f"{member.guild.id}:{member.id}:{day}").randint(0, 100), "random for today"
        return None, ""

    async def pick(self, ctx: commands.Context, text: str) -> discord.Member:
        for m in ctx.message.mentions:
            if isinstance(m, discord.Member) and not m.bot:
                return m
        ref = ctx.message.reference
        if ref is not None and not text.strip():
            msg = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if msg is not None:
                m = ctx.guild.get_member(msg.author.id)
                if m is not None and not m.bot:
                    return m
        first = text.split(" ", 1)[0] if text else ""
        if first.isdigit() and len(first) >= 15:
            m = ctx.guild.get_member(int(first))
            if m is not None:
                return m
        return ctx.author

    # ---------- $aura ----------
    @commands.group(name="aura", invoke_without_command=True)
    @commands.guild_only()
    async def aura(self, ctx: commands.Context, *, text: str = ""):
        """$aura [@member] - show the aura of a member (default: you)."""
        member = await self.pick(ctx, text)
        pct, _ = self.value(member)
        mention = discord.AllowedMentions.none()
        if pct is None:
            return await ctx.reply(f"{member.mention} has no aura set yet.", mention_author=False, allowed_mentions=mention)

        def line(p: int) -> str:
            return f"Aura this user holds! {member.mention}:\n`[{bar(p)}]` `{p}%`"
        msg = await ctx.reply(line(0), mention_author=False, allowed_mentions=mention)
        if pct <= 0:
            return
        steps = min(6, max(2, pct // 15))                    # the bar grows slowly: dotted blocks turn white one by one
        for i in range(1, steps + 1):
            await asyncio.sleep(0.8)
            try:
                await msg.edit(content=line(round(pct * i / steps)), allowed_mentions=mention)
            except discord.HTTPException:
                break

    @aura.command(name="config")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def aura_config(self, ctx: commands.Context):
        """Fix the aura of members / roles and turn random aura on / off."""
        view = AuraConfigView(self, ctx.author)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(Aura(bot))
