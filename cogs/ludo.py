"""Ludo - 2-4 players, a REAL board with animated pawns, played fully inside Discord.

$ludo                  open the lobby (works only in the Ludo channel)
$ludo set              (admin) pick the Ludo channel from a menu            - also: $ludo channel
$ludo stats [@member]  wins, kills, sixes
$ludo stop             stop the game in this channel (host or admin)

Rules: roll a 6 to bring a pawn out. A pawn walks 56 steps and must reach home with an EXACT roll.
Landing on an opponent (not on a star / start cell) kills it. A 6 or a kill gives an extra turn.
First player with all 4 pawns home wins. No valid move = the turn passes by itself.

Every animation (dice, walking pawn, kill, home, winner) is an animated GIF drawn by core/ludo_render.py.
The whole game state is saved in kon.db (settings key `ludo_games`), so a bot restart or a player coming back later
just continues - and it is part of every backup. Channel + stats live in kon.db too.
"""
import asyncio
import logging
import random
import time
from io import BytesIO
from typing import Optional

import discord
from discord.ext import commands, tasks

from core import ludo_render as R
from core.config import BRAND, PREFIX
from core.icons import I, err, ok
from core.minigames import GOLD, GREEN, RED, GameSettings, gate, is_admin, open_channel_picker

log = logging.getLogger("kon.ludo")

JOIN_ORDER = ("red", "yellow", "green", "blue")        # 2nd player sits opposite the 1st
BUTTON_STYLE = {"red": discord.ButtonStyle.danger, "green": discord.ButtonStyle.success,
                "blue": discord.ButtonStyle.primary, "yellow": discord.ButtonStyle.secondary}
ROLL_SECONDS = 90
PICK_SECONDS = 90
LOBBY_IDLE = 900
IDLE_END = 900               # everybody away for this long -> game closed
DONE_KEEP = 3600
NO_PING = discord.AllowedMentions.none()


def rgb_int(color: str) -> int:
    r, g, b = R.RGB[color]
    return (r << 16) | (g << 8) | b


def pawn_ic(color: str) -> str:
    return I.s(f"pawn_{color}")


def fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}m {sec % 60:02d}s"


# ============================================================================ pure game rules
def new_player(member, color: str) -> dict:
    return {"id": member.id, "name": member.display_name, "color": color, "pawns": [-1] * 4, "kills": 0, "lost": 0,
            "rolls": 0, "sixes": 0, "misses": 0, "away": False}


def valid_moves(st: dict) -> list:
    pl, d = st["players"][st["turn"]], st["dice"]
    out = []
    for k, p in enumerate(pl["pawns"]):
        if p == -1:
            if d == 6:
                out.append(k)
        elif p < 56 and p + d <= 56:
            out.append(k)
    return out


def apply_move(st: dict, k: int) -> dict:
    """Moves pawn k of the current player by the dice. Returns {from, to, killed:[(pi, k, p_before)], home}."""
    pi = st["turn"]
    pl = st["players"][pi]
    p0 = pl["pawns"][k]
    p1 = 0 if p0 == -1 else p0 + st["dice"]
    pl["pawns"][k] = p1
    killed = []
    if 0 <= p1 <= 50:
        idx = (R.START[pl["color"]] + p1) % 52
        if idx not in R.SAFE:
            for vi, vp in enumerate(st["players"]):
                if vi == pi:
                    continue
                for vk, pp in enumerate(vp["pawns"]):
                    if 0 <= pp <= 50 and (R.START[vp["color"]] + pp) % 52 == idx:
                        killed.append((vi, vk, pp))
                        vp["pawns"][vk] = -1
                        vp["lost"] += 1
                        pl["kills"] += 1
    return {"from": p0, "to": p1, "killed": killed, "home": p1 >= 56}


def next_turn(st: dict):
    n = len(st["players"])
    for step in range(1, n + 1):
        j = (st["turn"] + step) % n
        if not st["players"][j]["away"]:
            st["turn"] = j
            break
    else:
        st["turn"] = (st["turn"] + 1) % n
    st["dice"], st["valid"] = 0, []


def home_count(pl: dict) -> int:
    return sum(1 for p in pl["pawns"] if p >= 56)


# ============================================================================ views (persistent: one set of custom ids for every channel)
class LobbyView(discord.ui.View):
    def __init__(self, cog: "Ludo"):
        super().__init__(timeout=None)
        self.cog = cog
        for label, emoji, style, cid, cb in (
                ("Join Game", "user_add", discord.ButtonStyle.success, "ludo:join", cog.on_join),
                ("Leave", "logout", discord.ButtonStyle.secondary, "ludo:leave", cog.on_leave),
                ("Start Game", "play", discord.ButtonStyle.primary, "ludo:start", cog.on_start),
                ("Cancel", "cross", discord.ButtonStyle.secondary, "ludo:cancel", cog.on_cancel)):
            b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, custom_id=cid)
            b.callback = cb
            self.add_item(b)


class PlayView(discord.ui.View):
    """valid = pawn indexes that may be pressed (None = roll phase). locked = animation running -> everything off."""

    def __init__(self, cog: "Ludo", color: str = "blue", valid: Optional[list] = None, locked: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        roll = discord.ui.Button(label="Roll Dice", emoji=I.e("dice"), style=discord.ButtonStyle.success,
                                 custom_id="ludo:roll", disabled=locked or valid is not None)
        roll.callback = cog.on_roll
        self.add_item(roll)
        for k in range(4):
            b = discord.ui.Button(label=str(k + 1), emoji=I.e(f"pawn_{color}"), style=BUTTON_STYLE[color],
                                  custom_id=f"ludo:p{k}", disabled=locked or valid is None or k not in valid)
            b.callback = (lambda i, k=k: cog.on_pawn(i, k))
            self.add_item(b)
        for label, emoji, cid, cb in (("Rejoin", "refresh", "ludo:rejoin", cog.on_rejoin),
                                      ("Restart Game", "repeat", "ludo:restart", cog.on_restart),
                                      ("Quit Game", "logout", "ludo:quit", cog.on_quit)):
            b = discord.ui.Button(label=label, emoji=I.e(emoji), style=discord.ButtonStyle.secondary, custom_id=cid,
                                  row=1, disabled=locked and cid != "ludo:quit")
            b.callback = cb
            self.add_item(b)


class EndView(discord.ui.View):
    def __init__(self, cog: "Ludo"):
        super().__init__(timeout=None)
        a = discord.ui.Button(label="Play Again", emoji=I.e("repeat"), style=discord.ButtonStyle.success, custom_id="ludo:again")
        a.callback = cog.on_again
        q = discord.ui.Button(label="Quit Game", emoji=I.e("logout"), style=discord.ButtonStyle.secondary, custom_id="ludo:close")
        q.callback = cog.on_close
        self.add_item(a)
        self.add_item(q)


# ============================================================================ one game
class LudoGame:
    def __init__(self, cog: "Ludo", st: dict):
        self.cog, self.st = cog, st
        self.lock = asyncio.Lock()
        self.msg: Optional[discord.Message] = None
        self.channel: Optional[discord.abc.Messageable] = None
        self.dead = False

    # ----- embeds -----
    def lobby_embed(self) -> discord.Embed:
        st = self.st
        e = discord.Embed(title=f"{I.s('dice')} Ludo", color=BRAND, description=(
            f"A real Ludo board, live animations, fully inside Discord.\n\n"
            f"{I.s('dice')} Roll a **6** to bring a pawn out - then pick a pawn (**1-4**) to move it.\n"
            f"{I.s('skull')} Land on an opponent to **kill** it (not on a {I.s('star_fill')} star or start cell).\n"
            f"{I.s('bolt')} A **6** or a **kill** = extra turn.\n"
            f"{I.s('home')} Bring all 4 pawns home with an exact roll to win."))
        e.add_field(name=f"Players ({len(st['players'])}/4)", inline=False, value="\n".join(
            f"{pawn_ic(p['color'])} <@{p['id']}>" + (f" {I.s('crown')}" if p["id"] == st["host"] else "")
            for p in st["players"]))
        e.set_footer(text="2-4 players. The host presses Start Game - nothing starts by itself. Everyone else can watch live.")
        return e

    def board_embed(self, mode: str = "ready", fname: Optional[str] = None) -> discord.Embed:
        st = self.st
        cur = st["players"][st["turn"]]
        e = discord.Embed(title=f"{I.s('dice')} Ludo · {len(st['players'])} players", color=rgb_int(cur["color"]))
        who = f"{pawn_ic(cur['color'])} <@{cur['id']}>"
        if mode == "rolling":
            e.description = f"{I.s('dice')} {who} is rolling the dice..."
        elif mode == "moving":
            e.description = f"{who} rolled **{st['dice']}** - moving..."
        elif mode == "novalid":
            e.description = f"{I.s('dice')} {who} rolled **{st['dice']}** - **no valid move**, turn passes."
        elif st["dice"] and st["valid"]:
            dice = st["dice"]
            opts = "\n".join(f"{pawn_ic(cur['color'])} **Pawn {k + 1}** → " + ("out of base" if cur["pawns"][k] == -1 else f"+{dice} steps")
                             for k in st["valid"])
            e.description = f"{I.s('dice')} {who} rolled **{st['dice']}** - pick a pawn:\n{opts}"
        else:
            e.description = f"{I.s('bolt')} {who} - **it's your turn!** Press **Roll Dice**.\n-# {I.s('hourglass')} <t:{st['deadline']}:R>" if st.get("deadline") else f"{who} - it's your turn!"
        e.add_field(name="Players", inline=False, value="\n".join(
            f"{'▶ ' if i == st['turn'] else ''}{pawn_ic(p['color'])} <@{p['id']}> · {I.s('home')} {home_count(p)}/4 · {I.s('skull')} {p['kills']}"
            + (f" · {I.s('hourglass')} away" if p["away"] else "") for i, p in enumerate(st["players"])))
        if st.get("event"):
            e.add_field(name="Last move", value=st["event"], inline=False)
        e.set_footer(text="Spectators can watch live · 6 or a kill = extra turn · Away? press Rejoin")
        if fname:
            e.set_image(url=f"attachment://{fname}")
        return e

    def final_embed(self, winner: dict, fname: str, forfeit: bool = False) -> discord.Embed:
        st = self.st
        e = discord.Embed(title=f"{I.s('trophy')} Ludo - {winner['name']} wins!", color=GOLD)
        e.description = f"# {I.s('crown')} <@{winner['id']}>" + ("\n*All opponents left the game.*" if forfeit else "")
        ranked = sorted(st["players"], key=lambda p: (p is winner, home_count(p), p["kills"]), reverse=True)
        e.add_field(name="Final scoreboard", inline=False, value="\n".join(
            f"**{i}.** {pawn_ic(p['color'])} <@{p['id']}> · {I.s('home')} {home_count(p)}/4 · {I.s('skull')} {p['kills']} kills · "
            f"{I.s('dice')} {p['rolls']} rolls ({p['sixes']}×6)" for i, p in enumerate(ranked, 1)))
        e.add_field(name="Game stats", inline=False, value=(
            f"{I.s('skull')} Total kills: **{sum(p['kills'] for p in st['players'])}**\n"
            f"{I.s('dice')} Total rolls: **{sum(p['rolls'] for p in st['players'])}**\n"
            f"{I.s('hourglass')} Duration: **{fmt_time(st.get('ended', time.time()) - st['started'])}**"))
        e.set_image(url=f"attachment://{fname}")
        e.set_footer(text="Play Again keeps the same players and colours.")
        return e

    # ----- message plumbing -----
    async def show(self, data: bytes, ext: str, embed: discord.Embed, view, content: Optional[str] = None) -> bool:
        fname = f"ludo_{int(time.time() * 1000) % 10 ** 9}.{ext}"
        embed.set_image(url=f"attachment://{fname}")
        try:
            await self.msg.edit(content=content, embed=embed, attachments=[discord.File(BytesIO(data), filename=fname)],
                                view=view, allowed_mentions=NO_PING)
            return True
        except discord.NotFound:
            self.dead = True
            self.cog.drop(self)
            return False
        except discord.HTTPException as e:
            log.warning("ludo edit failed in %s: %s", self.st["cid"], e)
            return False

    def cur_color(self) -> str:
        return self.st["players"][self.st["turn"]]["color"]

    def ready_view(self) -> PlayView:
        return PlayView(self.cog, self.cur_color(), None)

    async def show_state(self, hl: bool = False, caption: str = "", ping: bool = False):
        """Static board for the current state (roll phase, or pawn-pick phase when hl)."""
        st = self.st
        valid = st["valid"] if st["dice"] and st["valid"] else None
        cur = st["turn"]
        ring = [(cur, k) for k in valid] if valid else None
        png = await asyncio.to_thread(R.still, st["players"], cur, st["dice"], caption or st.get("event", ""), ring)
        view = PlayView(self.cog, self.cur_color(), valid)
        await self.show(png, "png", self.board_embed(), view)
        self.cog.save(self)
        if ping:
            await self.ping_turn()

    async def ping_turn(self):
        st = self.st
        cur = st["players"][st["turn"]]
        if cur["away"] or self.channel is None:
            return
        try:
            await self.channel.send(f"{I.s('bolt')} <@{cur['id']}> it's your turn - roll the dice!", delete_after=12,
                                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(cur["id"])]))
        except discord.HTTPException:
            pass


# ============================================================================ cog
class Ludo(commands.Cog):
    """Ludo - 2-4 players, animated real board."""

    def __init__(self, bot):
        self.bot = bot
        self.settings = GameSettings(bot, "ludo")
        self.games: dict = {}                 # channel id -> LudoGame
        self._restored = False

    async def cog_load(self):
        for v in (LobbyView(self), PlayView(self), EndView(self)):
            self.bot.add_view(v)
        self.watch.start()

    def cog_unload(self):
        self.watch.cancel()

    # ----- storage (kon.db settings -> every backup) -----
    def save(self, g: LudoGame):
        d = self.bot.db.get_settings(g.st["gid"], "ludo_games")
        d[str(g.st["cid"])] = g.st
        self.bot.db.set_settings(g.st["gid"], "ludo_games", d)

    def drop(self, g: LudoGame):
        self.games.pop(g.st["cid"], None)
        d = self.bot.db.get_settings(g.st["gid"], "ludo_games")
        if d.pop(str(g.st["cid"]), None) is not None:
            self.bot.db.set_settings(g.st["gid"], "ludo_games", d)

    def save_stats(self, g: LudoGame, winner_id: int):
        gid = g.st["gid"]
        data = self.settings.stats(gid)
        for p in g.st["players"]:
            s = data.setdefault(str(p["id"]), {})
            for key in ("games", "wins", "kills", "deaths", "sixes", "rolls", "pawns_home"):
                s.setdefault(key, 0)
            s["games"] += 1
            s["wins"] += 1 if p["id"] == winner_id else 0
            s["kills"] += p["kills"]
            s["deaths"] += p["lost"]
            s["sixes"] += p["sixes"]
            s["rolls"] += p["rolls"]
            s["pawns_home"] += home_count(p)
        self.settings.save_stats(gid, data)

    def game_for(self, interaction: discord.Interaction) -> Optional[LudoGame]:
        g = self.games.get(interaction.channel_id)
        if g is not None and g.msg is None and interaction.message is not None:
            g.msg = interaction.message
        if g is not None and interaction.message is not None and g.msg is not None and g.msg.id != interaction.message.id:
            return None                                       # an old panel of a previous game
        return g

    async def say(self, interaction: discord.Interaction, text: str):
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def gone(self, interaction: discord.Interaction):
        await self.say(interaction, err("This game panel is no longer active."))

    async def is_staff(self, interaction: discord.Interaction, g: LudoGame) -> bool:
        u = interaction.user
        return u.id == g.st["host"] or u.guild_permissions.manage_guild or self.bot.is_owner_fast(u.id, interaction.guild_id)

    def player_index(self, g: LudoGame, uid: int) -> Optional[int]:
        for i, p in enumerate(g.st["players"]):
            if p["id"] == uid:
                return i
        return None

    # ============================================================== lobby
    async def on_join(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "lobby":
            return await self.gone(interaction)
        st = g.st
        if self.player_index(g, interaction.user.id) is not None:
            return await self.say(interaction, err("You are already in this game."))
        if len(st["players"]) >= 4:
            return await self.say(interaction, err("The game is full (4/4) - you can still watch live."))
        used = {p["color"] for p in st["players"]}
        color = next(c for c in JOIN_ORDER if c not in used)
        st["players"].append(new_player(interaction.user, color))
        self.save(g)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=LobbyView(self))

    async def on_leave(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "lobby":
            return await self.gone(interaction)
        i = self.player_index(g, interaction.user.id)
        if i is None:
            return await self.say(interaction, err("You are not in this game."))
        if interaction.user.id == g.st["host"]:
            return await self.say(interaction, err("You are the host - press **Cancel** to close the lobby."))
        g.st["players"].pop(i)
        self.save(g)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=LobbyView(self))

    async def on_cancel(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "lobby":
            return await self.gone(interaction)
        if not await self.is_staff(interaction, g):
            return await self.say(interaction, err("Only the host or an admin can cancel."))
        self.drop(g)
        e = discord.Embed(color=RED, title=f"{I.s('dice')} Ludo - lobby cancelled")
        await interaction.response.edit_message(embed=e, view=None)

    async def on_start(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "lobby":
            return await self.gone(interaction)
        st = g.st
        if not await self.is_staff(interaction, g):
            return await self.say(interaction, err("Only the host can start the game."))
        if len(st["players"]) < 2:
            return await self.say(interaction, err("Ludo needs **2-4 players** - wait for someone to press Join Game."))
        await interaction.response.defer()
        g.msg = interaction.message
        async with g.lock:
            await self.begin(g)

    async def begin(self, g: LudoGame):
        """Lobby/finished -> a fresh game with the same players (colours kept, clockwise turn order)."""
        st = g.st
        st["players"].sort(key=lambda p: R.COLORS.index(p["color"]))
        for p in st["players"]:
            p.update(pawns=[-1] * 4, kills=0, lost=0, rolls=0, sixes=0, misses=0, away=False)
        st.update(phase="play", turn=0, dice=0, valid=[], started=time.time(), last=time.time(), event="",
                  deadline=int(time.time()) + ROLL_SECONDS)
        st.pop("ended", None)
        await g.show_state(ping=True)

    # ============================================================== dice
    async def on_roll(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "play":
            return await self.gone(interaction)
        st = g.st
        i = self.player_index(g, interaction.user.id)
        if i is None:
            return await self.say(interaction, f"{I.s('show')} You're **spectating** - watch the game live! Ask the host for the next one.")
        if g.lock.locked():
            return await self.say(interaction, f"{I.s('hourglass')} Wait for the animation to finish.")
        if i != st["turn"]:
            return await self.say(interaction, err(f"It's <@{st['players'][st['turn']]['id']}>'s turn."))
        if st["dice"]:
            return await self.say(interaction, err("You already rolled - pick a pawn."))
        await interaction.response.defer()
        async with g.lock:
            if st["phase"] != "play" or st["dice"] or st["turn"] != i:
                return
            await self.roll(g)

    async def roll(self, g: LudoGame):
        st = g.st
        pl = st["players"][st["turn"]]
        pl["misses"], pl["away"], st["last"] = 0, False, time.time()
        value = random.randint(1, 6)
        pl["rolls"] += 1
        pl["sixes"] += 1 if value == 6 else 0
        st["dice"] = value
        st["valid"] = valid_moves(st)
        gif, secs = await asyncio.to_thread(R.roll_gif, st["players"], st["turn"], value, f"{R.clean(pl['name'])} rolls...")
        await g.show(gif, "gif", g.board_embed("rolling"), PlayView(self, pl["color"], None, locked=True))
        if g.dead:
            return
        await asyncio.sleep(min(secs, 3.2))
        if not st["valid"]:
            st["event"] = f"{pl['name']} rolled {value} - no valid move."
            png = await asyncio.to_thread(R.still, st["players"], st["turn"], value, "No valid move!", None, ("No valid move", (110, 110, 120)))
            await g.show(png, "png", g.board_embed("novalid"), PlayView(self, pl["color"], None, locked=True))
            await asyncio.sleep(2.2)
            next_turn(st)
            st["deadline"] = int(time.time()) + ROLL_SECONDS
            return await g.show_state(ping=True)
        st["deadline"] = int(time.time()) + PICK_SECONDS
        await g.show_state(hl=True)

    # ============================================================== pawn
    async def on_pawn(self, interaction: discord.Interaction, k: int):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "play":
            return await self.gone(interaction)
        st = g.st
        i = self.player_index(g, interaction.user.id)
        if i is None:
            return await self.say(interaction, f"{I.s('show')} You're **spectating** - watch the game live!")
        if g.lock.locked():
            return await self.say(interaction, f"{I.s('hourglass')} Wait for the animation to finish.")
        if i != st["turn"]:
            return await self.say(interaction, err(f"It's <@{st['players'][st['turn']]['id']}>'s turn."))
        if not st["dice"] or k not in st["valid"]:
            return await self.say(interaction, err("That pawn can't move now." if st["dice"] else "Roll the dice first."))
        await interaction.response.defer()
        async with g.lock:
            if st["phase"] != "play" or not st["dice"] or k not in st["valid"] or st["turn"] != i:
                return
            await self.move(g, k)

    async def move(self, g: LudoGame, k: int):
        st = g.st
        pi = st["turn"]
        pl = st["players"][pi]
        pl["misses"], pl["away"], st["last"] = 0, False, time.time()
        dice = st["dice"]
        res = apply_move(st, k)
        killed = res["killed"]
        name = R.clean(pl["name"])
        if killed:
            vnames = ", ".join(f"{st['players'][vi]['name']}'s pawn {vk + 1}" for vi, vk, _ in killed)
            st["event"] = f"{pl['name']} killed {vnames}!"
            cap = f"{name} killed {R.clean(st['players'][killed[0][0]]['name'])}!"
        elif res["home"]:
            st["event"] = f"{pl['name']}'s pawn {k + 1} reached HOME!"
            cap = f"{name}: pawn {k + 1} is HOME!"
        elif res["from"] == -1:
            st["event"] = f"{pl['name']} brought pawn {k + 1} out."
            cap = f"{name}: pawn {k + 1} is out"
        else:
            st["event"] = f"{pl['name']} moved pawn {k + 1} by {dice}."
            cap = f"{name}: pawn {k + 1} +{dice}"
        gif, secs = await asyncio.to_thread(R.move_gif, st["players"], st["turn"], dice, pi, k, res["from"], res["to"], killed, cap)
        await g.show(gif, "gif", g.board_embed("moving"), PlayView(self, pl["color"], None, locked=True))
        if g.dead:
            return
        await asyncio.sleep(min(secs, 4.5))
        if killed:                                                   # tell the victims (notification)
            for vi, vk, _ in killed:
                v = st["players"][vi]
                try:
                    await g.channel.send(f"{I.s('skull')} <@{v['id']}> your pawn **{vk + 1}** was killed by <@{pl['id']}>!",
                                         delete_after=15, allowed_mentions=discord.AllowedMentions(users=[discord.Object(v["id"])]))
                except discord.HTTPException:
                    pass
        if home_count(pl) == 4:
            return await self.finish(g, pi)
        if dice == 6 or killed:
            st["dice"], st["valid"] = 0, []
            st["event"] += " Extra turn!"
            st["deadline"] = int(time.time()) + ROLL_SECONDS
            return await g.show_state()
        next_turn(st)
        st["deadline"] = int(time.time()) + ROLL_SECONDS
        await g.show_state(ping=True)

    # ============================================================== end of game
    async def finish(self, g: LudoGame, pi: int, forfeit: bool = False):
        st = g.st
        w = st["players"][pi]
        st.update(phase="done", ended=time.time(), dice=0, valid=[], deadline=0)
        st["event"] = f"{w['name']} wins the game!"
        gif, _ = await asyncio.to_thread(R.win_gif, st["players"], pi, "")
        await g.show(gif, "gif", g.final_embed(w, "x.gif", forfeit), EndView(self), content=f"{I.s('trophy')} <@{w['id']}>")
        self.save_stats(g, w["id"])
        self.save(g)

    async def on_again(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "done":
            return await self.gone(interaction)
        if self.player_index(g, interaction.user.id) is None and not await self.is_staff(interaction, g):
            return await self.say(interaction, err("Only the players can start a rematch."))
        if g.lock.locked():
            return await self.say(interaction, f"{I.s('hourglass')} One moment...")
        await interaction.response.defer()
        g.msg = interaction.message
        async with g.lock:
            await self.begin(g)

    async def on_close(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None:
            return await self.gone(interaction)
        if self.player_index(g, interaction.user.id) is None and not await self.is_staff(interaction, g):
            return await self.say(interaction, err("Only the players can close this panel."))
        self.drop(g)
        await interaction.response.edit_message(view=None)

    async def on_restart(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "play":
            return await self.gone(interaction)
        if not await self.is_staff(interaction, g):
            return await self.say(interaction, err("Only the host or an admin can restart the game."))
        if g.lock.locked():
            return await self.say(interaction, f"{I.s('hourglass')} Wait for the animation to finish.")
        await interaction.response.defer()
        async with g.lock:
            await self.begin(g)
            try:
                await g.channel.send(f"{I.s('repeat')} **{interaction.user.display_name}** restarted the game - same players, fresh board!",
                                     delete_after=15, allowed_mentions=NO_PING)
            except discord.HTTPException:
                pass

    async def on_quit(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "play":
            return await self.gone(interaction)
        st = g.st
        i = self.player_index(g, interaction.user.id)
        staff = await self.is_staff(interaction, g)
        if i is None and not staff:
            return await self.say(interaction, err("You're spectating - nothing to quit."))
        if g.lock.locked():
            return await self.say(interaction, f"{I.s('hourglass')} Wait for the animation to finish.")
        await interaction.response.defer()
        async with g.lock:
            if st["phase"] != "play":
                return
            if staff and interaction.user.id == st["host"] or (staff and i is None):
                self.drop(g)
                e = discord.Embed(color=RED, title=f"{I.s('stop')} Ludo - ended by {interaction.user.display_name}")
                try:
                    await g.msg.edit(embed=e, attachments=[], view=None)
                except discord.HTTPException:
                    pass
                return
            leaver = st["players"].pop(i)
            if i < st["turn"]:
                st["turn"] -= 1
            elif i == st["turn"]:
                st["dice"], st["valid"] = 0, []
                st["turn"] = st["turn"] % len(st["players"])
                st["deadline"] = int(time.time()) + ROLL_SECONDS
            st["turn"] %= max(1, len(st["players"]))
            st["event"] = f"{leaver['name']} quit the game."
            if len(st["players"]) < 2:
                return await self.finish(g, 0, forfeit=True)
            await g.show_state(ping=True)

    async def on_rejoin(self, interaction: discord.Interaction):
        g = self.game_for(interaction)
        if g is None or g.st["phase"] != "play":
            return await self.gone(interaction)
        i = self.player_index(g, interaction.user.id)
        if i is None:
            return await self.say(interaction, f"{I.s('show')} You're not a player in this game - you can watch it live.")
        pl = g.st["players"][i]
        if not pl["away"]:
            cur = g.st["players"][g.st["turn"]]
            return await self.say(interaction, f"{I.s('check')} You're in the game ({pawn_ic(pl['color'])} {pl['color'].title()}). "
                                               f"Turn: <@{cur['id']}>.")
        pl["away"], pl["misses"] = False, 0
        g.st["last"] = time.time()
        self.save(g)
        await self.say(interaction, f"{I.s('check')} Welcome back! You're **{pl['color'].title()}** - your pawns are exactly where you left them.")
        if not g.lock.locked():
            async with g.lock:
                await g.show_state()

    # ============================================================== timers
    @tasks.loop(seconds=5)
    async def watch(self):
        now = time.time()
        for g in list(self.games.values()):
            st = g.st
            if g.lock.locked() or g.dead:
                continue
            try:
                if st["phase"] == "lobby" and now - st["created"] > LOBBY_IDLE:
                    self.drop(g)
                    if g.msg:
                        await g.msg.edit(embed=discord.Embed(color=RED, title=f"{I.s('dice')} Ludo - lobby expired"), view=None)
                elif st["phase"] == "done" and now - st.get("ended", now) > DONE_KEEP:
                    self.drop(g)
                    if g.msg:
                        await g.msg.edit(view=None)
                elif st["phase"] == "play":
                    if all(p["away"] for p in st["players"]) and now - st["last"] > IDLE_END:
                        self.drop(g)
                        if g.msg:
                            await g.msg.edit(embed=discord.Embed(color=RED, title=f"{I.s('dice')} Ludo - closed (everyone left)"), view=None)
                    elif st.get("deadline") and now > st["deadline"]:
                        asyncio.create_task(self.timeout(g))
            except discord.HTTPException:
                pass
            except Exception:
                log.exception("ludo watcher")

    @watch.before_loop
    async def _before_watch(self):
        await self.bot.wait_until_ready()
        await self.restore()

    async def timeout(self, g: LudoGame):
        if g.lock.locked():
            return
        async with g.lock:
            st = g.st
            if st["phase"] != "play" or not st.get("deadline") or time.time() <= st["deadline"]:
                return
            pl = st["players"][st["turn"]]
            pl["misses"] += 1
            if pl["misses"] >= 2:
                pl["away"] = True
            st["event"] = f"{pl['name']} didn't play in time - turn skipped" + (" (marked away - they can press Rejoin)." if pl["away"] else ".")
            next_turn(st)
            st["deadline"] = int(time.time()) + ROLL_SECONDS
            await g.show_state(ping=True)

    async def restore(self):
        """After a restart: every saved game gets its message re-attached and re-drawn, so play simply continues."""
        if self._restored:
            return
        self._restored = True
        for guild in self.bot.guilds:
            saved = self.bot.db.get_settings(guild.id, "ludo_games")
            for cid, st in list(saved.items()):
                try:
                    ch = guild.get_channel(int(cid)) or await self.bot.fetch_channel(int(cid))
                    msg = await ch.fetch_message(st["msg"])
                except Exception:
                    saved.pop(cid, None)
                    self.bot.db.set_settings(guild.id, "ludo_games", saved)
                    continue
                g = LudoGame(self, st)
                g.msg, g.channel = msg, ch
                self.games[int(cid)] = g
                if st["phase"] == "play":
                    st["deadline"] = int(time.time()) + ROLL_SECONDS            # fresh time after the downtime
                    asyncio.create_task(self._redraw(g))
        log.info("ludo: %d game(s) restored", len(self.games))

    async def _redraw(self, g: LudoGame):
        async with g.lock:
            await g.show_state()

    # ============================================================== commands
    @commands.hybrid_group(name="ludo", fallback="play", description="Play Ludo - 2-4 players, animated board")
    @commands.guild_only()
    async def ludo(self, ctx: commands.Context):
        if not await gate(ctx, label="Ludo", icon="dice", command="ludo", cid=self.settings.channel(ctx.guild.id),
                          extra_perms=("attach_files",)):
            return
        old = self.games.get(ctx.channel.id)
        if old is not None and old.st["phase"] != "done":
            return await ctx.reply(err(f"A Ludo game is already running here - [jump to it]({old.msg.jump_url})." if old.msg
                                       else "A Ludo game is already running here."), mention_author=False, ephemeral=True)
        if old is not None:
            self.drop(old)
        st = {"gid": ctx.guild.id, "cid": ctx.channel.id, "host": ctx.author.id, "phase": "lobby", "created": time.time(),
              "players": [new_player(ctx.author, "red")], "turn": 0, "dice": 0, "valid": [], "event": "", "deadline": 0,
              "started": 0, "last": time.time()}
        g = LudoGame(self, st)
        g.channel = ctx.channel
        self.games[ctx.channel.id] = g
        g.msg = await ctx.reply(embed=g.lobby_embed(), view=LobbyView(self), mention_author=False)
        st["msg"] = g.msg.id
        self.save(g)

    @ludo.command(name="channel", aliases=["set"], description="(Admin) Pick the channel Ludo works in")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def ludo_channel(self, ctx: commands.Context):
        await open_channel_picker(ctx, label="Ludo", icon="dice", command="ludo", current=self.settings.channel(ctx.guild.id),
                                  save=self.settings.set_channel, clear=self.settings.clear_channel, extra_perms=("attach_files",))

    @ludo.command(name="stop", description="Stop the Ludo game in this channel (host or admin)")
    @commands.guild_only()
    async def ludo_stop(self, ctx: commands.Context):
        g = self.games.get(ctx.channel.id)
        if g is None:
            return await ctx.reply(err("No Ludo game is running here."), mention_author=False, ephemeral=True)
        if ctx.author.id != g.st["host"] and not await is_admin(ctx):
            return await ctx.reply(err("Only the host or an admin can stop the game."), mention_author=False, ephemeral=True)
        self.drop(g)
        if g.msg:
            try:
                await g.msg.edit(embed=discord.Embed(color=RED, title=f"{I.s('stop')} Ludo - stopped"), attachments=[], view=None)
            except discord.HTTPException:
                pass
        await ctx.reply(f"{I.s('stop')} Ludo stopped.", mention_author=False)

    @ludo.command(name="stats", description="Your Ludo record")
    @commands.guild_only()
    async def ludo_stats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        s = self.settings.stats(ctx.guild.id).get(str(member.id))
        if not s or not s.get("games"):
            return await ctx.reply(f"{I.s('dice')} {member.display_name} hasn't played Ludo yet - start with `{PREFIX}ludo`.", mention_author=False)
        e = discord.Embed(color=BRAND, title=f"{I.s('dice')} Ludo stats - {member.display_name}")
        e.set_thumbnail(url=member.display_avatar.url)
        for name, key, ic in (("Wins", "wins", "trophy"), ("Games", "games", "gamepad"), ("Kills", "kills", "skull"),
                              ("Pawns killed", "deaths", "heart_broken"), ("Sixes", "sixes", "dice"), ("Pawns home", "pawns_home", "home")):
            e.add_field(name=f"{I.s(ic)} {name}", value=str(s.get(key, 0)), inline=True)
        await ctx.reply(embed=e, mention_author=False)


async def setup(bot):
    await bot.add_cog(Ludo(bot))
