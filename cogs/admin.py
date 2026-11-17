"""Setup commands: $customvc, $interface, $sync."""
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import checks
from core.config import PREFIX, VC_PREFIX
from core.config import GREEN
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import OwnedView


class CustomVCConfig(OwnedView):
    """$customvc config - members who can't be vc-banned / kicked / muted."""

    def __init__(self, svc, invoker: discord.Member):
        super().__init__(invoker.id, timeout=600)
        self.svc, self.guild = svc, invoker.guild
        self.render()

    @property
    def cfg(self) -> dict:
        return self.svc.cfg(self.guild.id)

    def embed(self) -> discord.Embed:
        c = self.cfg
        roles = " ".join(f"<@&{r}>" for r in c["roles"][:20]) or "*none*"
        return discord.Embed(title=f"{I.s('shield_check')} Custom VC protection", color=GREEN, description=(
            f"These can't be **banned, kicked or muted** in a custom VC:\n"
            f"{I.s('crown')} Server owner and Leafy owners (always)\n"
            f"{I.s('shield_check')} Admins: **{'protected' if c['admins'] else 'not protected'}**\n"
            f"{I.s('tag')} Roles: {roles}\n"
            f"-# Leafy owners can use every VC command in any custom VC."))

    def render(self):
        self.clear_items()

        def btn(label, emoji, cb, row, style=discord.ButtonStyle.secondary):
            b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, row=row)
            b.callback = cb
            self.add_item(b)
        btn("Add Roles", "plus", self.add_roles, 0, discord.ButtonStyle.primary)
        btn("Remove Roles", "minus", self.remove_roles, 0)
        btn(f"Admins: {'ON' if self.cfg['admins'] else 'OFF'}", "shield_check", self.t_admins, 1,
            discord.ButtonStyle.success if self.cfg["admins"] else discord.ButtonStyle.danger)
        btn("Close", "cross", self.close, 1)

    async def refresh(self):
        if self.message:
            self.render()
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass

    async def add_roles(self, interaction: discord.Interaction):
        view = discord.ui.View(timeout=120)
        sel = discord.ui.RoleSelect(placeholder="Pick roles...", min_values=1, max_values=10)

        async def picked(i: discord.Interaction):
            added = [r for r in sel.values if not r.is_default() and r.id not in self.cfg["roles"]]
            self.cfg["roles"] += [r.id for r in added]
            self.svc.save_cfg(self.guild.id)
            await i.response.edit_message(content=ok(f"Added {len(added)} role(s).") if added else err("Nothing new."), view=None)
            await self.refresh()
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Which roles should be protected?", view=view, ephemeral=True)

    async def remove_roles(self, interaction: discord.Interaction):
        opts = []
        for rid in self.cfg["roles"][:25]:
            r = self.guild.get_role(rid)
            opts.append(discord.SelectOption(label=(f"@{r.name}" if r else f"deleted role {rid}")[:100], value=str(rid)))
        if not opts:
            return await interaction.response.send_message(err("No roles added yet."), ephemeral=True)
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder="Pick roles to remove...", options=opts, min_values=1, max_values=len(opts))

        async def picked(i: discord.Interaction):
            gone = {int(v) for v in sel.values}
            self.cfg["roles"] = [r for r in self.cfg["roles"] if r not in gone]
            self.svc.save_cfg(self.guild.id)
            await i.response.edit_message(content=ok("Removed."), view=None)
            await self.refresh()
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Which roles should lose protection?", view=view, ephemeral=True)

    async def t_admins(self, interaction: discord.Interaction):
        self.cfg["admins"] = not self.cfg["admins"]
        self.svc.save_cfg(self.guild.id)
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=None)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    # ---------- $customvc ----------
    @commands.group(name="customvc", aliases=['cvc'], invoke_without_command=True)
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
                    f"{I.s('info')} Join-to-create is {cur.mention}. Change it: `{PREFIX}customvc #voice-channel`\n"
                    f"Rules: `{PREFIX}customvc config`", mention_author=False)
            try:
                cat = await ctx.guild.create_category("Custom VCs", reason="Custom VC setup")
                channel = await ctx.guild.create_voice_channel("➕ Join to Create", category=cat, reason="Custom VC setup")
            except discord.HTTPException:
                raise VCError("I couldn't create it. I need **Manage Channels**.")
        g["create"] = channel.id
        self.store.save_soon()
        await ctx.reply(ok(f"Join-to-create is now {channel.mention}."), mention_author=False)

    @customvc.command(name="config")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def customvc_config(self, ctx: commands.Context):
        """Choose who can't be vc-banned, kicked or muted."""
        view = CustomVCConfig(self.bot.vc_service, ctx.author)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)

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
        await ctx.reply(ok(f"Panel posted in {channel.mention}."), mention_author=False)

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
                text = f"Synced **{n}** commands here."
            elif scope == "clear":
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                text = "Slash commands cleared here."
            else:
                n = len(await self.bot.tree.sync())
                self.store.data["sync_hash"] = self.bot.command_hash()
                self.store.save_soon()
                text = f"Synced **{n}** commands globally."
        await ctx.reply(ok(text), mention_author=False)


async def setup(bot):
    await bot.add_cog(Admin(bot))
