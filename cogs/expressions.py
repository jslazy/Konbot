"""$ae ($addemoji) and $as ($addsticker) - steal emojis / stickers from any message.

    reply to a message that has custom emojis  ->  $ae       (adds them to this server)
    reply to a message that has a sticker      ->  $as [new name]

After adding, the message has two buttons: **Undo** (delete everything that command just added) and **Remove**
(pick which of them to delete). Text versions:
    $ae undo  |  $ae remove <:emoji:> or name ...        $as undo  |  $as remove <sticker name>
"""
import asyncio
import io
import logging
import re

import aiohttp
import discord
from discord.ext import commands

from core.errors import VCError
from core.icons import I, err, ok
from core.ui import OwnedView, replied_message

log = logging.getLogger("kon.expressions")

CUSTOM = re.compile(r"<(a?):(\w{2,32}):(\d{15,25})>")
MAX_EMOJIS = 20                  # per command
EMOJI_LIMIT = 256 * 1024         # Discord's size limit for emojis
STICKER_LIMIT = 512 * 1024       # ... and for stickers


async def download(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                return await r.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


def safe_name(name: str, lo: int, hi: int) -> str:
    name = re.sub(r"\W", "_", name)[:hi]
    return name if len(name) >= lo else (name + "_" * lo)[:lo]


async def delete_items(items, reason: str) -> int:
    n = 0
    for it in items:
        try:
            await it.delete(reason=reason)
            n += 1
        except discord.NotFound:
            pass
        except discord.HTTPException:
            continue
        await asyncio.sleep(0.3)
    return n


class UndoView(OwnedView):
    """Undo / Remove buttons under the "Added ..." message."""

    def __init__(self, cog: "Expressions", ctx: commands.Context, kind: str, items: list, text: str):
        super().__init__(ctx.author.id, timeout=180)
        self.cog, self.ctx, self.kind, self.items, self.text = cog, ctx, kind, list(items), text
        self.undo_btn.emoji = I.e("refresh")
        self.remove_btn.emoji = I.e("minus")

    def alive(self):
        g = self.ctx.guild
        pool = {e.id for e in (g.emojis if self.kind == "emoji" else g.stickers)}
        self.items = [i for i in self.items if i.id in pool]
        return self.items

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.danger)
    async def undo_btn(self, interaction: discord.Interaction, _):
        items = self.alive()
        self.stop()
        await interaction.response.edit_message(content=f"{I.s('hourglass')} Removing...", view=None)
        n = await delete_items(items, f"undo by {interaction.user} ({interaction.user.id})")
        self.cog.last.pop((self.ctx.guild.id, self.kind), None)
        await interaction.edit_original_response(content=ok(f"Undone. Removed **{n}** {self.kind}{'s' if n != 1 else ''}."))

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.secondary)
    async def remove_btn(self, interaction: discord.Interaction, _):
        items = self.alive()[:25]
        if not items:
            return await interaction.response.send_message(err("Nothing left to remove."), ephemeral=True)
        opts = [discord.SelectOption(label=i.name[:100], value=str(i.id),
                                     emoji=i if self.kind == "emoji" else None) for i in items]
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder=f"Select {self.kind}s to remove...", options=opts, min_values=1, max_values=len(opts))

        async def picked(i: discord.Interaction):
            ids = {int(v) for v in sel.values}
            chosen = [x for x in items if x.id in ids]
            await i.response.edit_message(content=f"{I.s('hourglass')} Removing...", view=None)
            n = await delete_items(chosen, f"remove by {i.user} ({i.user.id})")
            self.items = [x for x in self.items if x.id not in ids]
            await i.edit_original_response(content=ok(f"Removed **{n}** {self.kind}{'s' if n != 1 else ''}."))
            if not self.alive() and self.message:
                self.stop()
                try:
                    await self.message.edit(view=None)
                except discord.HTTPException:
                    pass
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message(f"Remove which {self.kind}s?", view=view, ephemeral=True)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class Expressions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last: dict[tuple, list] = {}        # (guild id, "emoji" / "sticker") -> what the last command added

    async def text_remove(self, ctx: commands.Context, kind: str, words: str, undo: bool):
        """$ae undo / $ae remove ... / $as undo / $as remove ..."""
        pool = ctx.guild.emojis if kind == "emoji" else ctx.guild.stickers
        reason = f"${'ae' if kind == 'emoji' else 'as'} {'undo' if undo else 'remove'} by {ctx.author} ({ctx.author.id})"
        if undo:
            alive = {e.id for e in pool}
            targets = [i for i in self.last.get((ctx.guild.id, kind), []) if i.id in alive]
            if not targets:
                raise VCError(f"Nothing to undo. I only remember {kind}s added since I started.")
        else:
            ids = {int(i) for _, _, i in CUSTOM.findall(words)}
            names = {w.lower().strip(":") for w in words.split() if not CUSTOM.fullmatch(w)}
            targets = [e for e in pool if e.id in ids or e.name.lower() in names]
            if not targets:
                raise VCError(f"That {kind} isn't in this server.\nUse `${'ae' if kind == 'emoji' else 'as'} remove "
                              + ("<emoji or name>`" if kind == "emoji" else "<name>`"))
        async with ctx.typing():
            n = await delete_items(targets, reason)
        self.last.pop((ctx.guild.id, kind), None) if undo else None
        await ctx.reply(ok(f"{'Undone. ' if undo else ''}Removed **{n}** {kind}{'s' if n != 1 else ''}."), mention_author=False)

    @staticmethod
    def check_perms(ctx: commands.Context):
        me = ctx.guild.me.guild_permissions
        if not (me.manage_expressions or me.manage_emojis_and_stickers):
            raise VCError("I need **Manage Expressions**.")

    # ---------- $ae ----------
    @commands.command(name="addemoji", aliases=["ae", "addemoj"])
    @commands.has_permissions(manage_expressions=True)
    @commands.guild_only()
    async def addemoji(self, ctx: commands.Context, *, text: str = ""):
        """Reply to a message with custom emojis and type `$ae` to add them to this server."""
        self.check_perms(ctx)
        head, _, rest = text.strip().partition(" ")
        if head.lower() in ("undo", "remove", "delete", "del"):
            return await self.text_remove(ctx, "emoji", rest, undo=head.lower() == "undo")
        found: dict[int, tuple[str, bool]] = {}              # id -> (name, animated)
        sources = [text]
        msg = await replied_message(ctx)
        if msg is not None:
            sources.append(msg.content)
            for e in msg.embeds:                              # emojis inside embeds too
                sources += [e.title or "", e.description or ""]
            for r in msg.reactions:                           # ... and the reactions on it
                if isinstance(r.emoji, (discord.PartialEmoji, discord.Emoji)) and r.emoji.id:
                    found.setdefault(r.emoji.id, (r.emoji.name, r.emoji.animated))
        for src in sources:
            for anim, name, eid in CUSTOM.findall(src or ""):
                found.setdefault(int(eid), (name, bool(anim)))
        if not found:
            raise VCError("No custom emoji found. Reply to a message that has some and type `$ae`.")
        todo = list(found.items())[:MAX_EMOJIS]

        added, failed, added_obj = [], [], []
        async with ctx.typing():
            for eid, (name, animated) in todo:
                ext = "gif" if animated else "png"
                data = None
                for size in (128, 96, 64):                    # shrink until it fits Discord's 256 KB limit
                    data = await download(f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size={size}&quality=lossless")
                    if data is None or len(data) <= EMOJI_LIMIT:
                        break
                if data is None:
                    failed.append(f"`{name}` - couldn't download")
                    continue
                if len(data) > EMOJI_LIMIT:
                    failed.append(f"`{name}` - file too big")
                    continue
                try:
                    emoji = await ctx.guild.create_custom_emoji(
                        name=safe_name(name, 2, 32), image=data, reason=f"$ae by {ctx.author} ({ctx.author.id})")
                    added.append(str(emoji))
                    added_obj.append(emoji)
                except discord.HTTPException as e:
                    failed.append(f"`{name}` - {e.text or 'failed'}")
                    if e.code == 30008:                       # no free emoji slots left
                        failed.append("This server has no free emoji slots left.")
                        break
                await asyncio.sleep(0.4)

        lines = []
        if added:
            lines.append(ok(f"Added **{len(added)}** emoji{'s' if len(added) != 1 else ''}: {' '.join(added)}"))
        if failed:
            lines.append(err("Couldn't add:\n" + "\n".join(f"· {f}" for f in failed)))
        text_out = "\n".join(lines)
        if added_obj:
            self.last[(ctx.guild.id, "emoji")] = added_obj
            view = UndoView(self, ctx, "emoji", added_obj, text_out)
            view.message = await ctx.reply(text_out, mention_author=False, view=view)
        else:
            await ctx.reply(text_out, mention_author=False)

    # ---------- $as ----------
    @commands.command(name="addsticker", aliases=["as"])
    @commands.has_permissions(manage_expressions=True)
    @commands.guild_only()
    async def addsticker(self, ctx: commands.Context, *, name: str = ""):
        """Reply to a message with a sticker and type `$as [new name]` to add it to this server."""
        self.check_perms(ctx)
        head, _, rest = name.strip().partition(" ")
        if head.lower() in ("undo", "remove", "delete", "del"):
            return await self.text_remove(ctx, "sticker", rest, undo=head.lower() == "undo")
        msg = await replied_message(ctx)
        stickers = list(msg.stickers) if msg is not None else []
        stickers = stickers or list(ctx.message.stickers)
        if not stickers:
            raise VCError("No sticker found. Reply to a message that has one and type `$as`.")

        added, failed = [], []
        async with ctx.typing():
            for st in stickers[:3]:
                label = st.name
                if st.format is discord.StickerFormatType.lottie:
                    failed.append(f"`{label}` - Lottie stickers can't be added (Discord only allows this for partnered servers)")
                    continue
                data = await download(st.url)
                if data is None:
                    failed.append(f"`{label}` - couldn't download")
                    continue
                if len(data) > STICKER_LIMIT:
                    failed.append(f"`{label}` - file is bigger than 512 KB")
                    continue
                try:
                    full = await st.fetch()
                except discord.HTTPException:
                    full = None
                desc = (getattr(full, "description", None) or label)[:100]
                tag = getattr(full, "emoji", None) or "slight_smile"
                new_name = (name.strip() if name.strip() and len(stickers) == 1 else label)
                ext = "gif" if st.format is discord.StickerFormatType.gif else "png"
                try:
                    sticker = await ctx.guild.create_sticker(
                        name=safe_name(new_name, 2, 30), description=desc, emoji=str(tag),
                        file=discord.File(io.BytesIO(data), filename=f"sticker.{ext}"),
                        reason=f"$as by {ctx.author} ({ctx.author.id})")
                    added.append(sticker)
                except discord.HTTPException as e:
                    failed.append(f"`{label}` - {e.text or 'failed'}")
                    if e.code == 30039:                       # no free sticker slots
                        break

        lines = []
        if added:
            lines.append(ok("Added sticker " + ", ".join(f"**{s.name}**" for s in added) + "."))
        if failed:
            lines.append(err("Couldn't add:\n" + "\n".join(f"· {f}" for f in failed)))
        text_out = "\n".join(lines)
        if added:
            self.last[(ctx.guild.id, "sticker")] = list(added)
            view = UndoView(self, ctx, "sticker", added, text_out)
            view.message = await ctx.reply(text_out, mention_author=False, view=view)
        else:
            await ctx.reply(text_out, mention_author=False)


async def setup(bot):
    await bot.add_cog(Expressions(bot))
