"""$noprefix (commands without the prefix) and $alias (custom command shortcuts)."""
import re
from typing import Optional

import discord
from discord.ext import commands

from core.config import BLURPLE, PREFIX
from core.errors import VCError
from core.icons import I, ok

MAX_ALIASES = 50


class Prefixes(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # =================== $noprefix ===================
    @commands.group(name="noprefix", aliases=["np"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def noprefix(self, ctx: commands.Context):
        """Show no-prefix settings. `$noprefix role @Role` sets the no-prefix role."""
        c = self.bot.noprefix_cfg(ctx.guild.id)
        role = ctx.guild.get_role(c["role"]) if c["role"] else None
        users = " ".join(f"<@{u}>" for u in c["users"][:20]) or "none"
        e = discord.Embed(title=f"{I.s('bolt')} No Prefix", color=BLURPLE, description=(
            f"{I.s('crown')} **Role:** {role.mention if role else 'not set'}\n"
            f"{I.s('users')} **Members:** {users}\n\n"
            f"They can type commands **without** `{PREFIX}`, like `profile` or `vclock`.\n"
            f"`{PREFIX}noprefix role @Role` · `{PREFIX}noprefix clear` · `{PREFIX}noprefix add @user` · "
            f"`{PREFIX}noprefix remove @user`"))
        await ctx.reply(embed=e, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @noprefix.command(name="role")
    @commands.has_permissions(manage_guild=True)
    async def np_role(self, ctx: commands.Context, role: discord.Role):
        """$noprefix role @Role - members with this role don't need the prefix."""
        if role.is_default():
            raise VCError("Pick a real role, not @everyone.")
        c = self.bot.noprefix_cfg(ctx.guild.id)
        c["role"] = role.id
        self.bot.save_noprefix(ctx.guild.id)
        await ctx.reply(ok(f"No-prefix role is {role.mention}. They can skip `{PREFIX}` now."),
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @noprefix.command(name="clear", aliases=["off", "reset"])
    @commands.has_permissions(manage_guild=True)
    async def np_clear(self, ctx: commands.Context):
        """$noprefix clear - remove the no-prefix role."""
        c = self.bot.noprefix_cfg(ctx.guild.id)
        c["role"] = None
        self.bot.save_noprefix(ctx.guild.id)
        await ctx.reply(ok("No-prefix role removed."), mention_author=False)

    @noprefix.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def np_add(self, ctx: commands.Context, member: discord.Member):
        """$noprefix add @user - give one member no-prefix access."""
        c = self.bot.noprefix_cfg(ctx.guild.id)
        if member.id in c["users"]:
            raise VCError("They already have no-prefix.")
        c["users"].append(member.id)
        self.bot.save_noprefix(ctx.guild.id)
        await ctx.reply(ok(f"{member.mention} can skip `{PREFIX}` now."),
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @noprefix.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def np_remove(self, ctx: commands.Context, member: discord.Member):
        """$noprefix remove @user"""
        c = self.bot.noprefix_cfg(ctx.guild.id)
        if member.id not in c["users"]:
            raise VCError("They weren't added by hand.")
        c["users"].remove(member.id)
        self.bot.save_noprefix(ctx.guild.id)
        await ctx.reply(ok(f"{member.mention} needs `{PREFIX}` again."), mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    # =================== $alias ===================
    @commands.group(name="alias", aliases=["aliases"], invoke_without_command=True)
    @commands.guild_only()
    async def alias(self, ctx: commands.Context):
        """List this server's custom aliases. Admins: `$alias add <name> <command>`."""
        await ctx.invoke(self.alias_list)

    @alias.command(name="list")
    async def alias_list(self, ctx: commands.Context):
        """$alias list - custom aliases of this server."""
        a = self.bot.aliases(ctx.guild.id)
        lines = [f"`{PREFIX}{k}` → `{PREFIX}{v}`" for k, v in sorted(a.items())]
        e = discord.Embed(title=f"{I.s('link')} Command Aliases", color=BLURPLE,
                          description="\n".join(lines) or "No custom aliases yet.\n"
                          f"Create one: `{PREFIX}alias add pa vcpermit all`")
        e.set_footer(text="Built-in aliases are in $help")
        await ctx.reply(embed=e, mention_author=False)

    @alias.command(name="add", aliases=["create", "set"])
    @commands.has_permissions(manage_guild=True)
    async def alias_add(self, ctx: commands.Context, name: str, *, command: str):
        """$alias add <name> <command> - e.g. `$alias add pa vcpermit all`"""
        name = name.lower().lstrip(PREFIX)
        if not re.fullmatch(r"[a-z0-9_]{1,20}", name):
            raise VCError("Use letters, numbers and `_` only (max 20).")
        if self.bot.get_command(name) is not None:
            raise VCError(f"`{name}` is already a command.")
        command = command.strip().lstrip(PREFIX).strip()
        first = command.split()[0].lower() if command else ""
        if not first or self.bot.get_command(first) is None:
            raise VCError(f"`{first or command}` isn't a command. Check `{PREFIX}help`.")
        a = self.bot.aliases(ctx.guild.id)
        if name not in a and len(a) >= MAX_ALIASES:
            raise VCError(f"Max {MAX_ALIASES} aliases.")
        a[name] = command
        self.bot.save_aliases(ctx.guild.id)
        await ctx.reply(ok(f"`{PREFIX}{name}` runs `{PREFIX}{command}` now."), mention_author=False)

    @alias.command(name="remove", aliases=["delete", "del"])
    @commands.has_permissions(manage_guild=True)
    async def alias_remove(self, ctx: commands.Context, name: str):
        """$alias remove <name>"""
        name = name.lower().lstrip(PREFIX)
        a = self.bot.aliases(ctx.guild.id)
        if name not in a:
            raise VCError("No such alias.")
        del a[name]
        self.bot.save_aliases(ctx.guild.id)
        await ctx.reply(ok(f"Removed `{name}`."), mention_author=False)


async def setup(bot):
    await bot.add_cog(Prefixes(bot))
