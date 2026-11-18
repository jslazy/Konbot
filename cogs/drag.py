"""/dragme - ask a custom-VC owner to drag you into their channel."""
import discord
from discord import app_commands
from discord.ext import commands

from core import ratelimit
from core.config import BLURPLE, BRAND
from core.errors import VCError
from core.icons import I


def result_embed(title: str, text: str, color=None) -> discord.Embed:
    return discord.Embed(title=title, description=text, color=color, timestamp=discord.utils.utcnow())


class DragView(discord.ui.View):
    def __init__(self, requester: discord.Member, owner: discord.Member, channel: discord.VoiceChannel):
        super().__init__(timeout=60)
        self.requester, self.owner, self.channel = requester, owner, channel
        self.message: discord.Message | None = None
        self.allow.emoji = I.e("check")
        self.deny.emoji = I.e("cross")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                f"{I.s('cross')} Only {self.owner.mention} can answer this.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(
                    embed=result_embed("Drag Request", f"{I.s('hourglass')} No answer, so it expired."), view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success)
    async def allow(self, interaction: discord.Interaction, _):
        self.stop()
        await interaction.response.edit_message(view=None)
        r, ch = self.requester, self.channel
        guild = interaction.guild
        live = guild.get_channel(ch.id)
        still_ok = (
            live is not None
            and r.voice and r.voice.channel
            and self.owner.voice and self.owner.voice.channel
            and self.owner.voice.channel.id == ch.id
        )
        if not still_ok:
            return await interaction.followup.send(embed=result_embed(
                "Drag Request",
                f"{I.s('cross')} Couldn't move them. Someone left voice."))
        try:
            await r.move_to(ch, reason=f"Drag request approved by {self.owner}")
        except discord.HTTPException:
            return await interaction.followup.send(embed=result_embed(
                "Drag Request",
                f"{I.s('cross')} Couldn't move them. Someone left voice."))
        await interaction.followup.send(embed=result_embed(
            "Drag Request",
            f"{I.s('check')} Moved **{r.name}** to **{ch.name}**.", BRAND))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _):
        self.stop()
        await interaction.response.edit_message(
            embed=result_embed("Drag Request",
                               f"{I.s('cross')} **{self.owner.display_name}** said no."),
            view=None)


class Drag(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    @commands.hybrid_command(name="dragme", aliases=['drag'], description="Request to be dragged to another user's VC")
    @app_commands.describe(user="Member whose voice channel you want to join")
    @commands.guild_only()
    async def dragme(self, ctx: commands.Context, user: discord.Member):
        me = ctx.author
        if not me.voice or not me.voice.channel:
            raise VCError("Join a voice channel first.")
        if user.id == me.id or user.bot:
            raise VCError("Pick another member (not a bot).")
        if not user.voice or not user.voice.channel:
            raise VCError("They're not in a voice channel.")
        ch = user.voice.channel
        rec = self.store.vc(ch.id)
        if not rec:
            raise VCError("They're not in a custom VC.")
        if me.voice.channel.id == ch.id:
            raise VCError("You're already there.")
        if me.id in rec["banned"]:
            raise VCError("You're banned from that VC.")
        if rec.get("block_drags"):
            raise VCError("That VC owner blocked drag requests.")
        owner = ctx.guild.get_member(rec["owner"])
        if owner is None:
            raise VCError("I can't find that VC's owner.")
        ra = ratelimit.check("drag", me.id)
        if ra:
            raise VCError(f"Try again in **{ratelimit.fmt(ra)}**.")

        if owner.id == me.id:  # (can't happen: you'd already be in it) kept for safety
            return
        embed = discord.Embed(
            title="Drag Request",
            description=(f"{me.mention} wants to join **{ch.name}**.\n"
                         f"{owner.mention}, allow it?"),
            color=BLURPLE, timestamp=discord.utils.utcnow())
        view = DragView(me, owner, ch)
        view.message = await ctx.reply(
            content=owner.mention, embed=embed, view=view, mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=[owner], replied_user=False))


async def setup(bot):
    await bot.add_cog(Drag(bot))
