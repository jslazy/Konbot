"""$games - one overview of every game: which channel it works in and how to start it."""
import discord
from discord.ext import commands

from core.config import BRAND, PREFIX
from core.icons import I


class Games(commands.Cog):
    """All games at a glance."""

    def __init__(self, bot):
        self.bot = bot

    def channels(self, gid: int) -> dict:
        bt = self.bot.get_cog("BlackTea")
        get = lambda key: self.bot.db.get_settings(gid, key).get("channel")
        return {"tictactoe": get("tictactoe"),
                "blacktea": bt.game_channel(gid) if bt else None,
                "timetap": get("timetap"), "handfight": get("handfight")}

    @commands.hybrid_command(name="games", aliases=["gamechannels"], description="All games and the channel each one works in")
    @commands.guild_only()
    async def games(self, ctx: commands.Context):
        ch = self.channels(ctx.guild.id)
        rows = (("Tic Tac Toe", "gamepad", "tictactoe", f"`{PREFIX}ttt play @member`"),
                ("BlackTea", "leaf", "blacktea", f"`{PREFIX}blacktea` · word bomb, up to 20 players"),
                ("TimeTap", "stopwatch", "timetap", f"`{PREFIX}timetap` · reaction duel: Training, Duo, Trio, Squad"),
                ("Handfight", "rock", "handfight", f"`{PREFIX}handfight` · rock paper scissors: Duo, Squad"))
        lines = []
        for name, icon, key, how in rows:
            where = f"<#{ch[key]}>" if ch[key] else "*not set*"
            lines.append(f"{I.s(icon)} **{name}** - {where}\n-# {how} · set channel: `{PREFIX}{key} set`")
        e = discord.Embed(title=f"{I.s('gamepad')} Games", color=BRAND, description="\n\n".join(lines))
        e.set_footer(text="Every game works only in its own channel. Admins pick it with  <game> set  (a channel menu opens).")
        await ctx.reply(embed=e, mention_author=False)


async def setup(bot):
    await bot.add_cog(Games(bot))
