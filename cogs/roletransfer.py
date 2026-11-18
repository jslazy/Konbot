"""$transfer - move roles from one member to another (Kon admins only).

    $transfer @from @to                 moves EVERY role @from has (that Kon and you are allowed to manage)
    $transfer @from @to @Role1 @Role2   moves only those roles

Who may use it: the bot owner, the members added with `$konextra` in this server, and server Administrators.
(`$vctransfer` is the command that gives your custom VC to someone else.)
"""
import discord
from discord.ext import commands

from core.config import BLURPLE, BRAND
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import OwnedView


def kon_admin():
    """Bot owner, per-server Kon owner ($konextra) or server Administrator."""
    async def predicate(ctx: commands.Context):
        gid = ctx.guild.id if ctx.guild else None
        if await ctx.bot.is_owner_in(ctx.author, gid):
            return True
        if ctx.guild and ctx.author.guild_permissions.administrator:
            return True
        raise commands.CheckFailure("not allowed")      # no rights = no reply
    return commands.check(predicate)


def role_list(roles, limit=25) -> str:
    text = " ".join(r.mention for r in roles[:limit])
    return text + (f" +{len(roles) - limit} more" if len(roles) > limit else "")


class ConfirmView(OwnedView):
    def __init__(self, ctx, source: discord.Member, target: discord.Member, roles: list, skipped: list):
        super().__init__(ctx.author.id, timeout=60)
        self.ctx, self.source, self.target, self.roles, self.skipped = ctx, source, target, roles, skipped
        self.confirm.emoji = I.e("check")
        self.cancel.emoji = I.e("cross")

    def preview(self) -> discord.Embed:
        e = discord.Embed(title=f"{I.s('swap')} Transfer roles", color=BLURPLE, description=(
            f"**From:** {self.source.mention}\n**To:** {self.target.mention}\n\n"
            f"**{len(self.roles)} role(s) will move:**\n{role_list(self.roles)}"))
        if self.skipped:
            e.add_field(name=f"{I.s('warning')} Skipped ({len(self.skipped)})", inline=False,
                        value="\n".join(self.skipped[:8])[:1024])
        e.set_footer(text="Confirm within 60 seconds.")
        return e

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _):
        self.stop()
        await interaction.response.edit_message(embed=discord.Embed(
            description=f"{I.s('hourglass')} Moving roles...", color=BLURPLE), view=None)
        reason = f"$transfer by {self.ctx.author} ({self.ctx.author.id})"
        try:
            await self.target.add_roles(*self.roles, reason=reason)
            await self.source.remove_roles(*self.roles, reason=reason)
        except discord.Forbidden:
            return await interaction.edit_original_response(embed=discord.Embed(
                description=err("I can't move them. I need **Manage Roles** and a higher role."),
                color=0xED4245))
        except discord.HTTPException as e:
            return await interaction.edit_original_response(embed=discord.Embed(
                description=err(f"Discord said no: {e.text or e.status}"), color=0xED4245))
        e = discord.Embed(title=f"{I.s('check')} Roles moved", color=BRAND, description=(
            f"{self.source.mention} → {self.target.mention}\n\n{role_list(self.roles)}"))
        if self.skipped:
            e.add_field(name=f"{I.s('warning')} Skipped ({len(self.skipped)})", inline=False,
                        value="\n".join(self.skipped[:8])[:1024])
        await interaction.edit_original_response(embed=e)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _):
        self.stop()
        await interaction.response.edit_message(embed=discord.Embed(description=f"{I.s('cross')} Cancelled.",
                                                                    color=BLURPLE), view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(embed=discord.Embed(description=f"{I.s('hourglass')} Expired.",
                                                            color=BLURPLE), view=None)
            except discord.HTTPException:
                pass


class RoleTransfer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="transfer", aliases=["roletransfer", "rtransfer"])
    @commands.guild_only()
    @kon_admin()
    async def transfer(self, ctx: commands.Context, source: discord.Member, target: discord.Member,
                       roles: commands.Greedy[discord.Role]):
        """$transfer @from @to [@roles...] - move roles from one member to another (Kon admins)."""
        if source.id == target.id:
            raise VCError("Pick two different members.")
        if target.bot:
            raise VCError("I can't move roles onto a bot.")
        g, me = ctx.guild, ctx.guild.me
        if not me.guild_permissions.manage_roles:
            raise VCError("I need **Manage Roles**.")

        bypass = ctx.author.id == g.owner_id or await self.bot.is_owner_in(ctx.author, g.id)
        wanted = list(roles) or [r for r in source.roles if not r.is_default()]
        movable, skipped = [], []
        for r in wanted:
            if r.is_default():
                continue
            if r not in source.roles:
                skipped.append(f"{r.mention} - {source.display_name} doesn't have it")
            elif r.managed:
                skipped.append(f"{r.mention} - managed by an integration")
            elif r >= me.top_role:
                skipped.append(f"{r.mention} - above my highest role")
            elif not bypass and r >= ctx.author.top_role:
                skipped.append(f"{r.mention} - above your highest role")
            else:
                movable.append(r)
        if not movable:
            raise VCError("There's no role I can move." + (f"\n{skipped[0]}" if skipped else ""))
        view = ConfirmView(ctx, source, target, movable, skipped)
        view.message = await ctx.reply(embed=view.preview(), view=view, mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(RoleTransfer(bot))
