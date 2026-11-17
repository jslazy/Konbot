"""Tic Tac Toe - only in the channel an admin sets.

    /tictactoe admin #channel      (admins) set the dedicated channel for the games     ($tictactoe admin #channel)
    /tictactoe play @member        challenge a member - works ONLY in that channel      ($tictactoe play @member, $ttt play @member)

The challenged member has 60 seconds to accept ("The challenge expired." afterwards). Each move has 90 seconds, the
player who runs out of time forfeits. Buttons are the 3x3 board (X blue, O red).
"""
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import ratelimit
from core.config import BLURPLE, GREEN
from core.errors import VCError
from core.icons import I, err, ok

KEY = "tictactoe"
LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
MOVE_TIMEOUT = 90
CHALLENGE_TIMEOUT = 60


def winner_line(board: list) -> Optional[tuple]:
    for a, b, c in LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return (a, b, c)
    return None


class ChallengeView(discord.ui.View):
    """Plain-text challenge (no embed): "@opponent, @challenger challenged you to a game of Tic Tac Toe." + Accept / Deny."""

    def __init__(self, cog: "TicTacToe", challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.cog, self.challenger, self.opponent = cog, challenger, opponent
        self.message: Optional[discord.Message] = None
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in (self.opponent.id, self.challenger.id):
            return True
        await interaction.response.send_message(err("That challenge isn't yours."), ephemeral=True)
        return False

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message(err("Only the challenged member can accept."), ephemeral=True)
        self.done = True
        self.stop()
        game = GameView(self.cog, self.challenger, self.opponent)
        self.cog.busy[self.challenger.id] = self.cog.busy[self.opponent.id] = True
        game.message = interaction.message
        # the game embed (title + "A (X) vs B (O)") is there from the very first move
        await interaction.response.edit_message(content=game.turn_text(), embed=game.embed(), view=game)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _):
        self.done = True
        self.stop()
        self.cog.busy.pop(self.challenger.id, None)
        text = "Challenge declined." if interaction.user.id == self.opponent.id else "Challenge cancelled."
        await interaction.response.edit_message(content=text, embed=None, view=None)

    async def on_timeout(self):
        if self.done or not self.message:
            return
        self.cog.busy.pop(self.challenger.id, None)
        try:
            await self.message.edit(content="The challenge expired.", embed=None, view=None)
        except discord.HTTPException:
            pass


class Cell(discord.ui.Button):
    def __init__(self, idx: int):
        super().__init__(label="\u200b", style=discord.ButtonStyle.secondary, row=idx // 3)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        await self.view.play(interaction, self.idx)


class GameView(discord.ui.View):
    MARK = ("X", "O")

    def __init__(self, cog: "TicTacToe", p1: discord.Member, p2: discord.Member):
        super().__init__(timeout=MOVE_TIMEOUT)
        self.cog = cog
        self.players = (p1, p2)                 # p1 = X (starts), p2 = O
        self.board = [None] * 9
        self.turn = 0
        self.over = False
        self.message: Optional[discord.Message] = None
        self.cells = [Cell(i) for i in range(9)]
        for c in self.cells:
            self.add_item(c)

    def embed(self) -> discord.Embed:
        p1, p2 = self.players
        return discord.Embed(title="Tic Tac Toe", color=BLURPLE,
                             description=f"**{p1.display_name}** (X) vs **{p2.display_name}** (O)")

    def turn_text(self) -> str:
        return f"{self.players[self.turn].mention} your turn ({self.MARK[self.turn]})"

    def finish(self):
        self.over = True
        for p in self.players:
            self.cog.busy.pop(p.id, None)
        for c in self.children:
            c.disabled = True
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (p.id for p in self.players):
            await interaction.response.send_message(err("You're not in this game."), ephemeral=True)
            return False
        return await ratelimit.guard_interaction(interaction, "button")

    async def play(self, interaction: discord.Interaction, idx: int):
        if interaction.user.id != self.players[self.turn].id:
            return await interaction.response.send_message(err("Not your turn."), ephemeral=True)
        if self.board[idx]:
            return await interaction.response.send_message(err("That square is taken."), ephemeral=True)
        mark = self.MARK[self.turn]
        self.board[idx] = mark
        cell = self.cells[idx]
        cell.label = mark
        cell.style = discord.ButtonStyle.primary if mark == "X" else discord.ButtonStyle.danger     # X blue, O red
        cell.disabled = True
        if winner_line(self.board):
            win = self.players[self.turn]
            self.finish()
            return await interaction.response.edit_message(content=f"{win.mention} wins!", embed=self.embed(), view=self)
        if all(self.board):
            self.finish()
            return await interaction.response.edit_message(content="It's a draw!", embed=self.embed(), view=self)
        self.turn ^= 1
        await interaction.response.edit_message(content=self.turn_text(), embed=self.embed(), view=self)

    async def on_timeout(self):
        if self.over:
            return
        loser, win = self.players[self.turn], self.players[self.turn ^ 1]
        self.finish()
        if self.message:
            try:
                await self.message.edit(content=f"{loser.mention} ran out of time. {win.mention} Won the game!",
                                        embed=self.embed(), view=self)
            except discord.HTTPException:
                pass


class TicTacToe(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.busy: dict[int, bool] = {}          # user id -> in a game / open challenge

    def channel_id(self, gid: int) -> Optional[int]:
        return self.bot.db.get_settings(gid, KEY).get("channel")

    # ---------- $tictactoe ----------
    @commands.hybrid_group(name="tictactoe", aliases=["ttt"], description="Tic Tac Toe", invoke_without_command=True)
    @commands.guild_only()
    async def tictactoe(self, ctx: commands.Context):
        """$tictactoe play @member - challenge someone (only in the Tic Tac Toe channel)."""
        ch = self.channel_id(ctx.guild.id)
        where = f"<#{ch}>" if ch else "*not set. An admin can use* `/tictactoe admin`"
        await ctx.reply(embed=discord.Embed(color=BLURPLE, title=f"{I.s('gamepad')} Tic Tac Toe", description=(
            f"**Channel:** {where}\n`/tictactoe play @member` or `$ttt play @member`")), mention_author=False)

    @tictactoe.command(name="admin", description="Set the dedicated channel for Tic Tac Toe games")
    @app_commands.describe(channel="Channel where Tic Tac Toe can be played")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def ttt_admin(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            raise VCError(f"I need **Send Messages** and **Embed Links** in {channel.mention}.")
        self.bot.db.set_settings(ctx.guild.id, KEY, {"channel": channel.id})
        await ctx.reply(ok(f"Tic Tac Toe now lives in {channel.mention}."), mention_author=False)

    @tictactoe.command(name="play", description="Challenge a member to Tic Tac Toe")
    @app_commands.describe(opponent="Member you want to play against")
    @commands.guild_only()
    async def ttt_play(self, ctx: commands.Context, opponent: discord.Member):
        ch = self.channel_id(ctx.guild.id)
        if not ch:
            raise VCError("Not set up yet. An admin needs to run `/tictactoe admin #channel`.")
        if ctx.channel.id != ch:
            raise VCError(f"Play it in <#{ch}>.")
        me = ctx.author
        if opponent.bot:
            raise VCError("Bots can't play. Pick a member.")
        if opponent.id == me.id:
            raise VCError("You can't play yourself.")
        if self.busy.get(me.id):
            raise VCError("You're already in a game.")
        if self.busy.get(opponent.id):
            raise VCError(f"{opponent.display_name} is already in a game.")
        self.busy[me.id] = True
        view = ChallengeView(self, me, opponent)
        try:
            view.message = await ctx.reply(
                content=f"{opponent.mention}, {me.mention} challenged you to a game of Tic Tac Toe.",
                mention_author=False, view=view, allowed_mentions=discord.AllowedMentions(users=[opponent, me]))
        except Exception:
            self.busy.pop(me.id, None)
            raise


async def setup(bot):
    await bot.add_cog(TicTacToe(bot))
