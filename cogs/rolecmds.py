"""$giverole ($gr) - turn any word into a command that gives a role.

    $giverole setmember @Member          ->  from now on  `$setmember @person`  gives @person the Member role
                                             (or REPLY to the person's message and just send `$setmember`)
    $giverole setvip @VIP @Staff         ->  same, but members with @Staff may use it too
    $giverole list  /  $giverole remove setmember
"""
import re
from typing import Optional

import discord
from discord.ext import commands

from core.config import BLURPLE, PREFIX
from core.errors import VCError
from core.icons import I, ok

MAX_ROLECMDS = 25
MAX_TARGETS = 10
RESERVED = {"list", "remove", "delete", "del", "clear"}


class RoleCmds(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    async def _replied_member(ctx: commands.Context) -> Optional[discord.Member]:
        """The member whose message this command replies to (None if it isn't a reply / not a server member)."""
        ref = ctx.message.reference
        if ref is None or ref.message_id is None:
            return None
        msg = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if msg is None:
            chan = ctx.guild.get_channel_or_thread(ref.channel_id) if ref.channel_id else None
            try:
                msg = await (chan or ctx.channel).fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        if msg.webhook_id:
            return None
        author = msg.author
        if isinstance(author, discord.Member) and author.guild.id == ctx.guild.id:
            return author
        member = ctx.guild.get_member(author.id)
        if member is None:
            try:
                member = await ctx.guild.fetch_member(author.id)
            except discord.HTTPException:
                return None
        return member

    # ---------- the generated commands ($setmember @user  or  reply + $setmember) ----------
    # main.py rewrites "$setmember @user" -> "$rolecmd setmember @user"
    @commands.command(name="rolecmd", hidden=True)
    @commands.guild_only()
    async def rolecmd(self, ctx: commands.Context, name: str, members: commands.Greedy[discord.Member]):
        data = self.bot.rolecmds(ctx.guild.id).get(name.lower())
        if not data:
            raise VCError("That command doesn't exist any more.")
        role = ctx.guild.get_role(data["role"])
        if role is None:
            raise VCError(f"The role of `{PREFIX}{name}` was deleted. An admin must set it again with `{PREFIX}giverole`.")

        author = ctx.author
        allowed_id = data.get("allowed")
        via_allowed = bool(allowed_id) and any(r.id == allowed_id for r in author.roles)
        if author.id != ctx.guild.owner_id and not via_allowed:
            if not author.guild_permissions.manage_roles:
                need = ctx.guild.get_role(allowed_id) if allowed_id else None
                raise VCError("You need **Manage Roles**" + (f" or the **{need.name}** role" if need else "")
                              + f" to use `{PREFIX}{name}`.")
            if role >= author.top_role:
                raise VCError(f"Your highest role must be above {role.mention} to give it.")

        me = ctx.guild.me
        if not me.guild_permissions.manage_roles:
            raise VCError("I need the **Manage Roles** permission.")
        if role.managed or role >= me.top_role:
            raise VCError(f"I can't give {role.mention}. Move my role above it in Server Settings → Roles.")
        if not members:                                  # no @mention -> use the person you REPLIED to
            target = await self._replied_member(ctx)
            if target is not None:
                members = [target]
        if not members:
            raise VCError(f"Mention who gets the role (`{PREFIX}{name} @user`) or reply to their message with `{PREFIX}{name}`.")

        given, had, failed = [], [], []
        for m in members[:MAX_TARGETS]:
            if role in m.roles:
                had.append(m)
                continue
            try:
                await m.add_roles(role, reason=f"{PREFIX}{name} used by {author} ({author.id})")
                given.append(m)
            except discord.HTTPException:
                failed.append(m)

        lines = []
        if given:
            lines.append(ok(f"Gave {role.mention} to {' '.join(m.mention for m in given)}."))
        if had:
            lines.append(f"{I.s('info')} {' '.join(m.mention for m in had)} already ha{'s' if len(had) == 1 else 've'} it.")
        if failed:
            lines.append(f"{I.s('cross')} Couldn't give it to {' '.join(m.mention for m in failed)}.")
        await ctx.reply("\n".join(lines), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    # ---------- $giverole ----------
    @commands.group(name="giverole", aliases=["gr"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def giverole(self, ctx: commands.Context, name: Optional[str] = None,
                        role: Optional[discord.Role] = None, allowed: Optional[discord.Role] = None):
        """$giverole <command> <@role> [@who-can-use] - make `$<command> @user` (or a reply + `$<command>`) give that role"""
        if name is None:
            return await self.send_help(ctx)
        if role is None:
            raise VCError(f"I couldn't find that role. Use: `{PREFIX}giverole {name} @Role`.")

        name = name.lower().lstrip(PREFIX)
        if not re.fullmatch(r"[a-z0-9_]{1,20}", name):
            raise VCError("Command names can only use letters, numbers and `_` (max 20 characters).")
        if name in RESERVED or self.bot.get_command(name) is not None or name in self.bot.aliases(ctx.guild.id):
            raise VCError(f"`{name}` is already used. Pick another command name.")
        if role.is_default() or role.managed:
            raise VCError("Pick a normal role (not @everyone and not a bot/integration role).")
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            raise VCError("You can only link roles **below your highest role**.")
        if role >= ctx.guild.me.top_role:
            raise VCError(f"{role.mention} is above my role. Move my role higher in Server Settings → Roles first.")
        if allowed is not None and allowed.is_default():
            allowed = None

        cmds = self.bot.rolecmds(ctx.guild.id)
        if name not in cmds and len(cmds) >= MAX_ROLECMDS:
            raise VCError(f"You can have at most {MAX_ROLECMDS} role commands.")
        cmds[name] = {"role": role.id, "allowed": allowed.id if allowed else None}
        self.bot.save_rolecmds(ctx.guild.id)
        who = f"members with **Manage Roles**" + (f" or {allowed.mention}" if allowed else "")
        await ctx.reply(ok(f"Command active! `{PREFIX}{name} @user` (or reply to a message + `{PREFIX}{name}`) now gives {role.mention}.\n"
                           f"Who can use it: {who}."),
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def send_help(self, ctx: commands.Context):
        e = discord.Embed(title=f"{I.s('link')} Role Commands", color=BLURPLE, description=(
            f"Make your own command that gives a role.\n\n"
            f"`{PREFIX}giverole <command> <@role> [@who-can-use]`\n"
            f"Example: `{PREFIX}giverole setmember @Member` → then `{PREFIX}setmember @person` gives them **Member**.\n"
            f"**Reply works too:** reply to someone's message and send `{PREFIX}setmember` - the person you replied to gets the role.\n"
            f"Works for any role: `{PREFIX}giverole setvip @VIP`, `{PREFIX}giverole setstaff @Staff @Admin` ...\n\n"
            f"`{PREFIX}giverole list` · `{PREFIX}giverole remove <command>`\n"
            f"By default members with **Manage Roles** can use the command; add a 3rd role to let that role use it too."))
        await ctx.reply(embed=e, mention_author=False)

    @giverole.command(name="list")
    @commands.has_permissions(manage_guild=True)
    async def ar_list(self, ctx: commands.Context):
        """$giverole list - all role commands of this server"""
        cmds = self.bot.rolecmds(ctx.guild.id)
        lines = []
        for n, d in sorted(cmds.items()):
            r = ctx.guild.get_role(d["role"])
            a = ctx.guild.get_role(d["allowed"]) if d.get("allowed") else None
            lines.append(f"`{PREFIX}{n} @user` → {r.mention if r else '*deleted role*'}"
                         + (f" · also {a.mention}" if a else ""))
        e = discord.Embed(title=f"{I.s('link')} Role Commands", color=BLURPLE,
                          description="\n".join(lines) or f"None yet. Create one: `{PREFIX}giverole setmember @Member`")
        await ctx.reply(embed=e, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @giverole.command(name="remove", aliases=["delete", "del"])
    @commands.has_permissions(manage_guild=True)
    async def ar_remove(self, ctx: commands.Context, name: str):
        """$giverole remove <command>"""
        name = name.lower().lstrip(PREFIX)
        cmds = self.bot.rolecmds(ctx.guild.id)
        if name not in cmds:
            raise VCError("That role command doesn't exist.")
        del cmds[name]
        self.bot.save_rolecmds(ctx.guild.id)
        await ctx.reply(ok(f"`{PREFIX}{name}` removed."), mention_author=False)


async def setup(bot):
    await bot.add_cog(RoleCmds(bot))
