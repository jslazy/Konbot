"""Owner check that knows which server a command runs in."""
from discord.ext import commands


def is_owner():
    """Bot owner (everywhere) or a member added with `$leafyextra` IN THIS SERVER. Fails silently like commands.is_owner."""
    async def predicate(ctx: commands.Context):
        gid = ctx.guild.id if ctx.guild else None
        if await ctx.bot.is_owner_in(ctx.author, gid):
            return True
        raise commands.NotOwner("Only the bot owner can do this.")
    return commands.check(predicate)


def admin_or_owner(perm: str = "manage_guild"):
    """Members with the server permission `perm`, the bot owner or a $leafyextra owner of THIS server."""
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if getattr(ctx.author.guild_permissions, perm, False) or await ctx.bot.is_owner_in(ctx.author, ctx.guild.id):
            return True
        raise commands.MissingPermissions([perm])
    return commands.check(predicate)
