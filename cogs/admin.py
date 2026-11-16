"""Setup commands: $customvc, $interface, $sync."""
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import checks
from core.config import PREFIX, VC_PREFIX
from core.errors import VCError
from core.icons import I, ok


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    # ---------- $customvc ----------
    @commands.command(name="customvc", aliases=['cvc'])
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def customvc(self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None):
        """$customvc [#voice-channel] - set the join-to-create channel (empty = create one)"""
        await ctx.defer()
        g = self.store.guild(ctx.guild.id)
        if channel is None:
            cur = ctx.guild.get_channel(g.get("create", 0))
            if cur is not None:
                return await ctx.reply(
                    f"{I.s('info')} Join-to-create channel is {cur.mention}.\n"
                    f"Change it with `{PREFIX}customvc #voice-channel`.", mention_author=False)
            try:
                cat = await ctx.guild.create_category("Custom VCs", reason="Custom VC setup")
                channel = await ctx.guild.create_voice_channel("➕ Join to Create", category=cat, reason="Custom VC setup")
            except discord.HTTPException:
                raise VCError("I couldn't create the channel. I need **Manage Channels**.")
        g["create"] = channel.id
        self.store.save_soon()
        await ctx.reply(
            ok(f"Join-to-create channel set to {channel.mention}. Members who join it get a private `{VC_PREFIX}name` VC."),
            mention_author=False)

    # ---------- $interface ----------
    @commands.command(name="interface")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def interface(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """$interface [#channel] - post the voice control panel (default: this channel)"""
        await ctx.defer()
        channel = channel or ctx.channel
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links and perms.attach_files):
            raise VCError(f"I need **Send Messages, Embed Links and Attach Files** in {channel.mention}.")
        await self.bot.get_cog("Interface").post_panel(channel)
        await ctx.reply(ok(f"Interface channel set to {channel.mention}."), mention_author=False)

    # ---------- $sync ----------
    @commands.command(name="sync", hidden=True)
    @checks.is_owner()
    async def sync(self, ctx: commands.Context, scope: Optional[Literal["guild", "global", "clear"]] = None):
        """$sync            -> sync globally (up to 1h to show everywhere)
        $sync guild      -> sync to this server instantly
        $sync clear      -> remove this server's slash commands"""
        async with ctx.typing():
            if scope == "guild":
                self.bot.tree.copy_global_to(guild=ctx.guild)
                n = len(await self.bot.tree.sync(guild=ctx.guild))
                text = f"Synced **{n}** commands to this server."
            elif scope == "clear":
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                text = "Cleared this server's slash commands."
            else:
                n = len(await self.bot.tree.sync())
                self.store.data["sync_hash"] = self.bot.command_hash()
                self.store.save_soon()
                text = f"Synced **{n}** commands globally."
        await ctx.reply(ok(text), mention_author=False)


async def setup(bot):
    await bot.add_cog(Admin(bot))
