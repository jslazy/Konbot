"""$kon ping / info, $adminhelp (clean command list - everybody only sees what THEY can use) and $konextra."""
import inspect
import time
from typing import Optional

import platform

import discord
from discord import app_commands
from discord.ext import commands

from core import checks
from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, ok
from core.ui import OwnedView

CATEGORIES = {
    "VC": ("Custom VC", "speaker"), "Drag": ("Drag", "swap"), "Interface": ("Setup & Admin", "tools"),
    "AFK": ("AFK", "clock"), "Points": ("Points & Badges", "heart"), "Shop": ("Point Shop", "coins"),
    "Admin": ("Setup & Admin", "tools"), "Prefixes": ("No-Prefix & Aliases", "link"), "Backup": ("Backup", "gift_box"),
    "Meta": ("General", "home"), "Autoreact": ("Auto Reacter", "sparkles"), "RoleCmds": ("Role Commands", "crown"),
    "SelfRoles": ("Self Roles", "crown"), "Expressions": ("Emojis & Stickers", "sparkles"), "Purge": ("Cleanup", "tools"),
    "Aura": ("Aura", "fire"), "RoleTransfer": ("Role Transfer", "crown"), "TicTacToe": ("Tic Tac Toe", "gamepad"), "CmdControl": ("Command Control", "lock"),
}

# short, plain descriptions (key = command name without prefix). Anything missing falls back to the docstring.
DESC = {
    "vclock": "Lock your VC", "vcunlock": "Unlock your VC", "vchide": "Hide your VC", "vcunhide": "Show your VC again",
    "vckick": "Kick someone from your VC", "vcban": "Ban someone from your VC", "vcunban": "Unban someone",
    "vcpermit": "Let someone join your VC", "vcpermitall": "Permit everyone currently in your VC",
    "vctransfer": "Give your VC to someone else", "vcmute": "Mute someone for a while (default 10m)",
    "vclimit": "Set the user limit (0 = no limit)", "vcrename": "Rename your VC", "vcstatus": "Set the VC status text",
    "vcbitrate": "Change audio quality", "vcinfo": "Who owns this VC", "denydrags": "Block / allow drag requests to your VC",
    "dragme": "Ask a VC owner to drag you in",
    "afk": "Go AFK (optional reason, max 50 characters)", "afktag": "Turn the [AFK] nickname tag on/off",
    "profile": "Your points, chat and VC progress", "leaderboard": "Top 10 members by points",
    "store": "Open the point shop", "pointshop": "Open the point shop", "pointshop config": "Add / edit / reorder shop roles",
    "point": "Points settings overview", "point config": "Points, cooldown and channel rules",
    "point add": "Give points", "point remove": "Take points", "point set": "Set exact points", "point reset": "Wipe a member's data",
    "badge": "Show badges", "badge give": "Give a badge", "badge remove": "Remove a badge",
    "noprefix": "Show no-prefix settings", "noprefix role": "Role that doesn't need the prefix",
    "noprefix add": "Give one member no-prefix", "noprefix remove": "Remove a member's no-prefix",
    "noprefix clear": "Remove the no-prefix role",
    "alias": "List custom aliases", "alias list": "List custom aliases",
    "alias add": "Create an alias (e.g. `pa` = `vcpermit all`)", "alias remove": "Delete an alias",
    "giverole": "Make `$<name> @user` give a role", "giverole list": "List role commands", "giverole remove": "Delete a role command",
    "ar": "React with your emoji when someone pings you", "ar setup": "Same as `$ar <emoji>`",
    "ar off": "Turn your autoreact off", "ar config": "Roles that may use autoreact",
    "ar config add": "Give roles access", "ar config remove": "Take access from roles",
    "ar config clear": "Remove all roles",
    "selfrole": "Self-role overview", "selfrole setup": "Open the self-role editor", "setsr": "Set the self-role channel and post it",
    "editsr": "Edit banner, title, roles, emojis", "addemoji": "Reply to a message to add its emojis (Undo / Remove buttons)",
    "addsticker": "Reply to a message to add its sticker (Undo / Remove buttons)",
    "purgebot": "Delete bot + command messages (default 10, max 500)",
    "purgemember": "Delete a member's messages: `$p @member 50` or reply + `$p` (default 10, max 500)",
    "customvc": "Set the join-to-create channel", "customvc config": "Who can't be vc-banned, kicked or muted", "interface": "Post the voice control panel",
    "sync": "Sync slash commands", "backup": "Backup status", "backup refresh": "Back up right now",
    "backupload": "Attach data zip(s) to merge them into the bot", "backup config": "Send the readable config + players list",
    "konping": "Latency, uptime and status", "koninfo": "Servers, members and bot stats",
    "aura": "Show your aura (or @member's)", "aura config": "Fix aura, random on/off, switch a person off",
    "transfer": "Move roles from one member to another (Kon admins)",
    "tictactoe": "Tic Tac Toe info", "tictactoe play": "Challenge a member (only in the game channel)",
    "tictactoe admin": "Set the Tic Tac Toe channel",
    "blacktea": "Start a BlackTea word game (`$bt easy|medium|hard`)", "blacktea play": "Start a BlackTea word game",
    "blacktea channel": "Set the BlackTea channel", "setblacktea": "Set the BlackTea channel",
    "blacktea reset": "Remove the BlackTea channel", "blacktea stop": "Stop the running BlackTea game",
    "imgchannel": "Set the private channel that stores converted images", "imgchannelreset": "Remove the image channel",
    "tolink": "Turn an image / GIF / banner into a saved link (attach, reply or paste a link)",
    "links": "List saved image links", "linkget": "Fresh link + preview of a saved image", "linkdel": "Delete a saved image",
    "webhook": "Send through a webhook: `$webhook` editor or `$webhook #channel text` (emoji IDs = animated emojis)",
    "webhookedit": "Edit a message the webhook sent: `$whe <message link> new text`",
    "ei": "List animated emojis with their IDs (`$ei name`, `$ei here`, `$ei <id>`)",
    "point enable": "Count points in a channel again", "point disable": "Stop counting points in a channel",
    "disablecommand": "Turn commands off in channels / whole categories (`$db`, no arguments = menu)",
    "enablecommand": "Turn commands back on (`$eb`)",
    "adminhelp": "This menu", "konextra": "Give a member Kon owner rights (this server only)",
    "konextra remove": "Take owner rights away", "konextra list": "Show the extra owners",
}

SECTIONS = (("member", "Everyone"), ("admin", "Admins"), ("owner", "Owner"))


true_owner_only = checks.true_owner_only          # kept for the old name


def fmt_secs(s: int) -> str:
    d, r = divmod(int(s), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return " ".join(x for x in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s") if x)


async def access_level(ctx: commands.Context, cmd: commands.Command) -> Optional[str]:
    """None = this user can't use the command (so it is hidden). Else 'member' / 'admin' / 'owner'."""
    level = "member"
    c = cmd
    while c is not None:                                     # the command's own checks + its parent groups'
        for chk in c.checks:
            try:
                r = chk(ctx)
                if inspect.isawaitable(r):
                    r = await r
                allowed = bool(r)
            except Exception:
                allowed = False
            if not allowed:
                return None
            q = getattr(chk, "__qualname__", "").lower()
            if "owner" in q:
                level = "owner"
            elif ("has_permissions" in q or "kon_admin" in q) and level != "owner":
                level = "admin"
        c = c.parent
    return level


def describe(cmd: commands.Command) -> str:
    d = DESC.get(cmd.qualified_name)
    if d:
        return d
    d = (cmd.description or cmd.short_doc or "").split("\n")[0].strip()
    if d.startswith(PREFIX) and " - " in d:                  # "$cmd args - text"  ->  "text"
        d = d.split(" - ", 1)[1]
    return d if d and not d.startswith(PREFIX) else ""


class HelpView(OwnedView):
    def __init__(self, ctx, pages: dict):
        super().__init__(ctx.author.id, timeout=300)
        self.pages = pages          # key -> (title, icon, {level: [lines]})
        opts = [discord.SelectOption(label="Overview", value="__all__", emoji=I.e("menu"))]
        for key, (title, icon, secs) in pages.items():
            n = sum(len(v) for v in secs.values())
            opts.append(discord.SelectOption(label=title, value=key, description=f"{n} commands", emoji=I.e(icon)))
        sel = discord.ui.Select(placeholder="Pick a category...", options=opts[:25])
        sel.callback = self.picked
        self.sel = sel
        self.add_item(sel)

    def overview(self) -> discord.Embed:
        rows = [f"{I.s(icon)} **{title}** - {sum(len(v) for v in secs.values())}" for key, (title, icon, secs) in self.pages.items()]
        return discord.Embed(title=f"{I.s('sparkles')} Kon Help", color=BRAND,                 # same layout as Anshika Help
                             description=f"Prefix: `{PREFIX}`\n`<x>` = required · `[x]` = optional\n\n" + "\n".join(rows))

    def page(self, key) -> discord.Embed:
        title, icon, secs = self.pages[key]
        multi = sum(1 for v in secs.values() if v) > 1
        parts = []
        for level, label in SECTIONS:
            lines = secs.get(level)
            if not lines:
                continue
            parts.append((f"-# {label}\n" if multi else "") + "\n".join(lines))
        text = "\n".join(parts) or "—"
        return discord.Embed(title=f"{I.s(icon)} {title}", color=BRAND, description=text[:4000])

    async def picked(self, interaction: discord.Interaction):
        v = self.sel.values[0]
        await interaction.response.edit_message(embed=self.overview() if v == "__all__" else self.page(v), view=self)


class InfoView(OwnedView):
    PER_PAGE = 5

    def __init__(self, cog, ctx, owner: bool):
        super().__init__(ctx.author.id, timeout=300)
        self.cog, self.bot, self.owner = cog, ctx.bot, owner
        self.mode, self.page = "overview", 0
        self.render()

    def guilds(self):
        return sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)

    def overview(self) -> discord.Embed:
        b = self.bot
        gs = b.guilds
        members = sum(g.member_count or 0 for g in gs)
        humans = sum(1 for u in b.users if not u.bot)
        channels = sum(len(g.channels) for g in gs)
        lines = [
            f"{I.s('folder')} **Servers:** `{len(gs):,}`",
            f"{I.s('users')} **Members:** `{members:,}` (unique `{humans:,}`)",
            f"{I.s('message')} **Channels:** `{channels:,}`",
            f"{I.s('heart')} **Players:** `{b.db.counts()['users']:,}`",
            f"{I.s('signal')} **Latency:** `{b.latency * 1000:.0f} ms`",
            f"{I.s('clock')} **Uptime:** `{fmt_secs(time.time() - self.cog.started)}`",
            f"{I.s('terminal')} **Python:** `{platform.python_version()}` · **discord.py:** `{discord.__version__}`",
        ]
        return discord.Embed(title=f"{I.s('sparkles')} Kon Info", color=BRAND, description="\n".join(lines))

    def servers(self) -> discord.Embed:
        gs = self.guilds()
        pages = max(1, -(-len(gs) // self.PER_PAGE))
        self.page = max(0, min(self.page, pages - 1))
        chunk = gs[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]
        lines = [f"**{self.page * self.PER_PAGE + i}.** **{g.name}**\n{I.s('users')} **Members:** `{g.member_count or 0:,}`\n"
                 f"{I.s('tag')} **ID:** `{g.id}`" for i, g in enumerate(chunk, 1)]
        e = discord.Embed(title=f"{I.s('folder')} Kon is in {len(gs)} server(s)", color=BLURPLE,
                          description="\n\n".join(lines) or "—")
        e.set_footer(text=f"Page {self.page + 1}/{pages} · {sum(g.member_count or 0 for g in gs):,} members in total")
        return e

    def render(self):
        self.clear_items()

        def btn(label, emoji, cb, style=discord.ButtonStyle.secondary, disabled=False):
            b = discord.ui.Button(label=label, emoji=I.e(emoji) if emoji else None, style=style, disabled=disabled)
            b.callback = cb
            self.add_item(b)
        btn("Overview", "info", self.go_overview, discord.ButtonStyle.primary if self.mode == "overview" else discord.ButtonStyle.secondary)
        if self.owner:
            pages = max(1, -(-len(self.bot.guilds) // self.PER_PAGE))
            btn("Servers", "folder", self.go_servers, discord.ButtonStyle.primary if self.mode == "servers" else discord.ButtonStyle.secondary)
            if self.mode == "servers":
                btn(None, "left", self.prev, disabled=self.page <= 0)
                btn(None, "right", self.next, disabled=self.page >= pages - 1)

    async def show(self, interaction):
        self.render()
        await interaction.response.edit_message(embed=self.overview() if self.mode == "overview" else self.servers(), view=self)

    async def go_overview(self, i):
        self.mode = "overview"
        await self.show(i)

    async def go_servers(self, i):
        self.mode, self.page = "servers", 0
        await self.show(i)

    async def prev(self, i):
        self.page -= 1
        await self.show(i)

    async def next(self, i):
        self.page += 1
        await self.show(i)


class Meta(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.started = time.time()

    # ---------- $kon / $konping / $koninfo ----------
    @commands.hybrid_command(name="konping", aliases=["kping"], description="Show Kon's latency and uptime")
    async def kon_ping(self, ctx: commands.Context):
        """$konping - latency, uptime and status."""
        ws = self.bot.latency * 1000
        t0 = time.perf_counter()
        msg = await ctx.reply(f"{I.s('refresh')} Pinging...", mention_author=False)
        api = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        self.bot.db.ping()
        db = (time.perf_counter() - t0) * 1000

        def dot(ms, good, ok_):
            return "🟢" if ms < good else ("🟡" if ms < ok_ else "🔴")
        worst = max(ws / 150, api / 300, db / 20)
        status, color = (("Active", BRAND) if worst < 1 else ("Slow", 0xF58219) if worst < 2.5 else ("Struggling", 0xED4245))
        e = discord.Embed(title=f"{I.s('sparkles')} Kon Ping", color=color, description=(
            f"{I.s('check')} **Status:** `{status}`\n"
            f"{I.s('signal')} **Gateway latency:** `{ws:.0f} ms` {dot(ws, 150, 300)}\n"
            f"{I.s('bolt')} **API latency:** `{api:.0f} ms` {dot(api, 300, 700)}\n"
            f"{I.s('folder')} **Database:** `{db:.1f} ms` {dot(db, 20, 100)}\n"
            f"{I.s('clock')} **Uptime:** `{fmt_secs(time.time() - self.started)}`"))
        await msg.edit(content=None, embed=e)

    @commands.hybrid_command(name="koninfo", aliases=["kinfo"], description="Servers, members and stats of Kon")
    async def kon_info(self, ctx: commands.Context):
        """$koninfo - how many servers / members Kon has (the server list is for the bot owner)."""
        owner = await self.bot.is_true_owner(ctx.author)
        view = InfoView(self, ctx, owner)
        view.message = await ctx.reply(embed=view.overview(), view=view, mention_author=False)

    # ---------- $adminhelp ----------
    @commands.command(name="adminhelp", aliases=["help", "ahelp"])
    @commands.guild_only()
    async def adminhelp(self, ctx: commands.Context):
        """Commands you can use (admin / owner commands only show for admins / owners)."""
        pages: dict[str, tuple] = {}
        for cmd in sorted(self.bot.walk_commands(), key=lambda c: c.qualified_name):
            if cmd.name == "rolecmd":                      # internal dispatcher for custom role commands
                continue
            level = await access_level(ctx, cmd)
            if level is None:                              # not allowed -> never shown
                continue
            cog = cmd.cog_name or "Meta"
            title, icon = CATEGORIES.get(cog, (cog, "menu"))
            if cog in ("Interface", "Admin"):
                cog = "Admin"
            usage = f"{PREFIX}{cmd.qualified_name} {cmd.signature}".strip()
            desc = describe(cmd)
            line = f"`{usage}`" + (f" - {desc}" if desc else "")
            pages.setdefault(cog, (title, icon, {}))[2].setdefault(level, []).append(line)
        pages = dict(sorted(pages.items(), key=lambda kv: kv[1][0].lower()))      # A-Z like Anshika Help
        view = HelpView(ctx, pages)
        view.message = await ctx.reply(embed=view.overview(), view=view, mention_author=False)

    # ---------- $konextra (bot owner only, PER SERVER) ----------
    @commands.group(name="konextra", aliases=["kextra"], invoke_without_command=True)
    @commands.guild_only()
    @true_owner_only()
    async def konextra(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """$konextra @member - give that member Kon owner permission IN THIS SERVER only."""
        if member is None:
            return await ctx.invoke(self.konextra_list)
        if member.bot:
            raise VCError("A bot can't be an owner.")
        if await self.bot.is_true_owner(member):
            raise VCError(f"{member.mention} is already the bot owner.")
        extra = self.bot.extra_owners(ctx.guild.id)
        if member.id in extra:
            raise VCError(f"{member.mention} is already an owner here.")
        extra.append(member.id)
        self.bot.store.save_soon()
        await ctx.reply(ok(f"{member.mention} is a **Kon owner** in **{ctx.guild.name}** only. "
                           f"Undo: `{PREFIX}konextra remove @user`."),
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @konextra.command(name="remove", aliases=["delete", "del"])
    @commands.guild_only()
    @true_owner_only()
    async def konextra_remove(self, ctx: commands.Context, member: discord.User):
        """$konextra remove @member"""
        extra = self.bot.extra_owners(ctx.guild.id)
        if member.id not in extra:
            raise VCError(f"{member.mention} isn't an owner here.")
        extra.remove(member.id)
        self.bot.store.save_soon()
        await ctx.reply(ok(f"{member.mention} is no longer an owner here."), mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @konextra.command(name="list")
    @commands.guild_only()
    @true_owner_only()
    async def konextra_list(self, ctx: commands.Context):
        """$konextra list - extra owners of this server"""
        extra = self.bot.extra_owners(ctx.guild.id)
        text = "\n".join(f"<@{i}>" for i in extra) if extra else "*nobody yet*"
        e = discord.Embed(title=f"{I.s('crown')} Extra owners · {ctx.guild.name}", description=text, color=BLURPLE)
        e.set_footer(text=f"{PREFIX}konextra @user to add · {PREFIX}konextra remove @user · this server only")
        await ctx.reply(embed=e, mention_author=False, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Meta(bot))
