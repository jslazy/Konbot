"""Point shop: $store (role shop), item details + purchase, $pointshop config."""
import asyncio

import discord
from discord.ext import commands

from core import emoji as emojis
from core import media
from core.config import BLURPLE, BRAND
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import FieldsModal, OwnedView, parse_int, to_partial

RED = 0xED4245
MAX_ITEMS = 25


def stock_text(stock: int) -> str:
    if stock == 0:
        return "SOLD OUT"
    return "Unlimited" if stock < 0 else str(stock)


def stock_long(stock: int) -> str:
    if stock < 0:
        return "Unlimited"
    return f"{stock} remaining"


def safe_partial(bot, text):
    """Emoji for a select option. Deleted / unreachable custom emojis make Discord reject the whole
    message (50035 Invalid emoji), so a custom emoji is used only if the bot can actually see it."""
    pe = to_partial(text)
    if pe is None:
        return None
    if pe.id is not None and bot.get_emoji(pe.id) is None:
        return None
    return pe


class ItemView(discord.ui.View):
    """Ephemeral 'Item Details' with the purchase button."""

    def __init__(self, cog, item, role, member, balance):
        super().__init__(timeout=120)
        self.cog, self.item, self.member = cog, item, member
        owned = role in member.roles
        if item["stock"] == 0:
            label, disabled = "Out of Stock", True
        elif owned:
            label, disabled = "Already Owned", True
        else:
            label, disabled = "Purchase", False
        b = discord.ui.Button(label=label, style=discord.ButtonStyle.success, disabled=disabled,
                              emoji=None if disabled else I.e("coins"))
        b.callback = self.buy
        self.add_item(b)

    async def buy(self, interaction: discord.Interaction):
        self.stop()
        await self.cog.purchase(interaction, self.item["id"])


STORE_LINE = "───⬢⬡⬢──────⬢⬡⬢──────⬢⬡⬢───"          # same divider as the leaderboard
STORE_PER_PAGE = 5


class StoreSelect(discord.ui.Select):
    def __init__(self, cog, options):
        super().__init__(placeholder="Select a role to purchase...", options=options, min_values=1, max_values=1, row=1)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.show_item(interaction, int(self.values[0]))


class StoreView(OwnedView):
    """Leaderboard-style shop: 5 roles per page, Previous / Balance / Next and a select to buy."""

    def __init__(self, cog: "Shop", ctx: commands.Context):
        super().__init__(ctx.author.id, timeout=300)
        self.cog, self.guild, self.author = cog, ctx.guild, ctx.author
        self.page = 0
        self.emoji_ok = True                       # False = dropdown without emojis (Discord refused one)
        self.select = None
        self.prev.emoji, self.bal.emoji, self.next.emoji = I.e("left"), I.e("wallet"), I.e("right")
        self.reload()

    def entries(self) -> list:
        out = []
        for it in self.cog.db.shop_list(self.guild.id):
            role = self.guild.get_role(it["role_id"])
            if role is not None:
                out.append((it, role))
        return out

    def reload(self):
        self.all = self.entries()
        self.pages = max(1, -(-len(self.all) // STORE_PER_PAGE))
        self.page = min(max(self.page, 0), self.pages - 1)
        chunk = self.chunk()
        if self.select is not None:
            self.remove_item(self.select)
            self.select = None
        if chunk:
            opts = []
            for it, role in chunk:
                opts.append(discord.SelectOption(
                    label=(f"{role.name} (Sold Out)" if it["stock"] == 0 else role.name)[:100],
                    value=str(it["id"]),
                    description=f"Price: {it['price']} pts — Stock: {stock_text(it['stock'])}"[:100],
                    emoji=safe_partial(self.cog.bot, it["emoji"]) if self.emoji_ok else None))
            self.select = StoreSelect(self.cog, opts)
            self.add_item(self.select)
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1

    def chunk(self) -> list:
        return self.all[self.page * STORE_PER_PAGE:(self.page + 1) * STORE_PER_PAGE]

    def embed(self) -> discord.Embed:
        lines = [STORE_LINE]
        start = self.page * STORE_PER_PAGE
        for n, (it, role) in enumerate(self.chunk(), start + 1):
            owned = role in getattr(self.author, "roles", ())
            tag = f" {I.s('check')} Owned" if owned else (" · **SOLD OUT**" if it["stock"] == 0 else "")
            lines.append(f"**{n}.** {it['emoji'] or ''} {role.mention} — **{it['price']:,}** Points\n"
                         f"{I.s('gift_box')} Stock: `{stock_text(it['stock'])}`{tag}")
        if len(lines) == 1:
            lines.append("The shop is empty.")
        bal = self.cog.db.user(self.guild.id, self.author.id)["points"]
        desc = "\n".join(lines) + f"\n{STORE_LINE}\n{I.s('wallet')} **Your Balance:** {bal:,} Points"
        e = discord.Embed(title=f"{I.s('coins')} ∫ Role Shop", color=BLURPLE, description=desc[:4096])
        thumb = self.cog.shop_settings(self.guild.id).get("thumbnail") or (self.guild.icon.url if self.guild.icon else None)
        if thumb:
            e.set_thumbnail(url=thumb)
        e.set_footer(text=f"Page {self.page + 1}/{self.pages} • ∫ Kon")
        return e

    async def go(self, interaction: discord.Interaction, page: int):
        self.page = page
        self.reload()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.go(interaction, self.page - 1)

    @discord.ui.button(label="Balance", style=discord.ButtonStyle.secondary, row=0)
    async def bal(self, interaction: discord.Interaction, button: discord.ui.Button):
        bal = self.cog.db.user(self.guild.id, self.author.id)["points"]
        await interaction.response.send_message(f"{I.s('wallet')} You have **{bal:,}** points.", ephemeral=True)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.go(interaction, self.page + 1)


# ---------------- config panel ----------------
class ShopConfigView(OwnedView):
    def __init__(self, cog: "Shop", invoker: discord.Member):
        super().__init__(invoker.id, timeout=600)
        self.cog, self.guild = cog, invoker.guild
        for label, emoji, cb, style in (
            ("Add Item", "plus", self.add_item_btn, discord.ButtonStyle.success),
            ("Edit Item", "edit", self.edit_item_btn, discord.ButtonStyle.secondary),
            ("Remove Item", "minus", self.remove_item_btn, discord.ButtonStyle.danger),
            ("Position", "swap", self.position_btn, discord.ButtonStyle.primary),
            ("Thumbnail", "sparkles", self.thumb_btn, discord.ButtonStyle.secondary),
            ("Close", "cross", self.close_btn, discord.ButtonStyle.secondary),
        ):
            b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style)
            b.callback = cb
            self.add_item(b)

    def embed(self) -> discord.Embed:
        items = self.cog.db.shop_list(self.guild.id)
        lines = []
        for n, it in enumerate(items, 1):
            role = self.guild.get_role(it["role_id"])
            lines.append(f"`{n}.` {it['emoji'] or ''} {role.mention if role else '*deleted role*'} — "
                         f"`{it['price']}` pts · stock `{stock_text(it['stock'])}`")
        body = "\n".join(lines) if lines else "The shop is empty. Tap **Add Item**."
        body += f"\n-# Stock `-1` = unlimited, `0` = sold out · **Position** changes the order"
        e = discord.Embed(title="Shop Configuration", color=BRAND, description=body)
        return e

    async def refresh(self):
        if self.message:
            await self.message.edit(embed=self.embed(), view=self)

    # ----- add -----
    async def add_item_btn(self, interaction: discord.Interaction):
        if len(self.cog.db.shop_list(self.guild.id)) >= MAX_ITEMS:
            return await interaction.response.send_message(err(f"The shop is full ({MAX_ITEMS} items)."), ephemeral=True)
        view = discord.ui.View(timeout=120)
        sel = discord.ui.RoleSelect(placeholder="Select the role to sell...")

        async def picked(i: discord.Interaction):
            role = sel.values[0]
            if not role.is_assignable():
                return await i.response.send_message(
                    err("I can't give that role. Move my role above it."),
                    ephemeral=True)

            async def cb(i2, v):
                price = parse_int(v[0], 1, 10_000_000, "Price")
                stock = parse_int(v[1], -1, 1_000_000, "Stock")
                emoji = await emojis.normalize(self.cog.bot, self.guild, v[2])
                self.cog.db.shop_upsert(self.guild.id, role.id, price, stock, emoji)
                await i2.response.send_message(ok(f"{role.mention} added to the shop for **{price}** pts."),
                                               ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                await self.refresh()
            await i.response.send_modal(FieldsModal(f"Add: {role.name}"[:45], [
                ("Price (points)", "100", 8, True), ("Stock (-1 = unlimited)", "-1", 7, True),
                ("Emoji or emoji ID (optional)", "", 64, False)], cb))
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Which role?", view=view, ephemeral=True)

    # ----- edit / remove -----
    def _item_select(self, placeholder, handler):
        items = self.cog.db.shop_list(self.guild.id)
        if not items:
            return None
        opts = []
        for it in items[:25]:
            role = self.guild.get_role(it["role_id"])
            opts.append(discord.SelectOption(label=(role.name if role else f"deleted role {it['role_id']}")[:100],
                                             value=str(it["id"]), description=f"{it['price']} pts · stock {stock_text(it['stock'])}",
                                             emoji=safe_partial(self.cog.bot, it["emoji"])))
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder=placeholder, options=opts)

        async def picked(i):
            await handler(i, int(sel.values[0]))
        sel.callback = picked
        view.add_item(sel)
        return view

    async def edit_item_btn(self, interaction):
        async def handler(i: discord.Interaction, item_id: int):
            it = self.cog.db.shop_get(self.guild.id, item_id)
            if not it:
                return await i.response.send_message(err("That item is gone."), ephemeral=True)

            async def cb(i2, v):
                self.cog.db.shop_update(self.guild.id, item_id, parse_int(v[0], 1, 10_000_000, "Price"),
                                        parse_int(v[1], -1, 1_000_000, "Stock"),
                                        await emojis.normalize(self.cog.bot, self.guild, v[2]))
                await i2.response.send_message(ok("Updated."), ephemeral=True)
                await self.refresh()
            await i.response.send_modal(FieldsModal("Edit item", [
                ("Price (points)", str(it["price"]), 8, True), ("Stock (-1 = unlimited)", str(it["stock"]), 7, True),
                ("Emoji or emoji ID (optional)", it["emoji"] or "", 64, False)], cb))
        view = self._item_select("Select an item to edit...", handler)
        if view is None:
            return await interaction.response.send_message(err("The shop is empty."), ephemeral=True)
        await interaction.response.send_message("Which item?", view=view, ephemeral=True)

    async def remove_item_btn(self, interaction):
        async def handler(i: discord.Interaction, item_id: int):
            self.cog.db.shop_remove(self.guild.id, item_id)
            await i.response.edit_message(content=ok("Item removed."), view=None)
            await self.refresh()
        view = self._item_select("Select an item to remove...", handler)
        if view is None:
            return await interaction.response.send_message(err("The shop is empty."), ephemeral=True)
        await interaction.response.send_message("Remove which item?", view=view, ephemeral=True)

    async def position_btn(self, interaction):
        total = len(self.cog.db.shop_list(self.guild.id))

        async def handler(i: discord.Interaction, item_id: int):
            it = self.cog.db.shop_get(self.guild.id, item_id)
            if not it:
                return await i.response.send_message(err("That item is gone."), ephemeral=True)
            cur = next((n for n, x in enumerate(self.cog.db.shop_list(self.guild.id), 1) if x["id"] == item_id), 1)

            async def cb(i2, v):
                pos = self.cog.db.shop_move(self.guild.id, item_id, parse_int(v[0], 1, MAX_ITEMS, "Position"))
                await i2.response.send_message(ok(f"Moved to position **{pos}**."), ephemeral=True)
                await self.refresh()
            await i.response.send_modal(FieldsModal("Change position", [
                (f"New position (1 - {total})", str(cur), 3, True)], cb))
        view = self._item_select("Select the role to move...", handler)
        if view is None:
            return await interaction.response.send_message(err("The shop is empty."), ephemeral=True)
        await interaction.response.send_message("Move which role?", view=view, ephemeral=True)

    async def thumb_btn(self, interaction):
        """Upload an image (or paste a link; empty = back to the server icon)."""
        async def done(i, item):
            s = self.cog.shop_settings(self.guild.id)
            await media.forget(self.cog.bot, {"ref": s.get("thumbnail_ref")})
            s["thumbnail"] = item["url"] if item else ""
            s["thumbnail_ref"] = item["ref"] if item else None
            self.cog.db.set_settings(self.guild.id, "shop", s)
            await i.followup.send(ok("Thumbnail updated." if item else "Thumbnail reset to the server icon."), ephemeral=True)
        await interaction.response.send_modal(media.ImageModal(
            self.cog.bot, self.guild, "Shop thumbnail", done, label="Shop thumbnail", allow_clear=True,
            fallback=interaction.channel))

    async def close_btn(self, interaction):
        self.stop()
        await interaction.response.edit_message(view=None)


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._locks: dict[int, asyncio.Lock] = {}

    def shop_settings(self, gid: int) -> dict:
        return self.db.get_settings(gid, "shop")

    async def refresh_thumb(self, gid: int):
        """Ask Discord for a fresh link of an uploaded thumbnail (CDN links expire)."""
        s = self.shop_settings(gid)
        if s.get("thumbnail_ref"):
            item = {"url": s.get("thumbnail"), "ref": s["thumbnail_ref"]}
            url = await media.fresh(self.bot, item)
            if url and url != s.get("thumbnail"):
                s["thumbnail"] = url
                self.db.set_settings(gid, "shop", s)

    async def show_shop(self, ctx: commands.Context):
        await self.refresh_thumb(ctx.guild.id)
        view = StoreView(self, ctx)
        kw = dict(mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        try:
            view.message = await ctx.reply(embed=view.embed(), view=view, **kw)
        except discord.HTTPException as e:
            if e.code != 50035:
                raise
            view.emoji_ok = False                   # some emoji is invalid -> same shop, dropdown without emojis
            view.reload()
            view.message = await ctx.reply(embed=view.embed(), view=view, **kw)

    @commands.hybrid_command(name="store", aliases=["shop"], description="Open the point shop (buy roles with points)")
    @commands.guild_only()
    async def store(self, ctx: commands.Context):
        await self.show_shop(ctx)

    # ---------- item details ----------
    async def show_item(self, interaction: discord.Interaction, item_id: int):
        guild, member = interaction.guild, interaction.user
        it = self.db.shop_get(guild.id, item_id)
        role = guild.get_role(it["role_id"]) if it else None
        if not it or role is None:
            return await interaction.response.send_message(err("That item isn't available anymore."), ephemeral=True)
        bal = self.db.user(guild.id, member.id)["points"]
        sold_out = it["stock"] == 0
        embed = discord.Embed(
            title=f"{it['emoji'] or ''} Item Details: {role.name}".strip(),
            description=(f"### Price\n{I.s('coins')} {it['price']} Points\n"
                         f"### Stock\n{I.s('gift_box')} {stock_long(it['stock'])}\n"
                         f"### Your Balance\n{I.s('wallet')} {bal} Points\n\n"
                         f"No refunds after you tap Purchase."),
            color=RED if sold_out else BRAND)
        await interaction.response.send_message(embed=embed, view=ItemView(self, it, role, member, bal), ephemeral=True)

    # ---------- purchase ----------
    async def purchase(self, interaction: discord.Interaction, item_id: int):
        guild, member = interaction.guild, interaction.user
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            it = self.db.shop_get(guild.id, item_id)
            role = guild.get_role(it["role_id"]) if it else None
            if not it or role is None:
                return await interaction.response.edit_message(content=err("That item isn't available anymore."),
                                                               embed=None, view=None)
            if role in member.roles:
                return await interaction.response.edit_message(content=err("You already own that role."), embed=None, view=None)
            if not role.is_assignable():
                return await interaction.response.edit_message(
                    content=err("I can't give that role right now. Ask an admin to move my role up."),
                    embed=None, view=None)
            status, item, bal = self.db.buy(guild.id, member.id, item_id)
            if status == "soldout":
                return await interaction.response.edit_message(content=err("Sold out just now."), embed=None, view=None)
            if status == "poor":
                return await interaction.response.edit_message(
                    content=err(f"You're **{item['price'] - bal}** points short (you have **{bal}**)."), embed=None, view=None)
            if status != "ok":
                return await interaction.response.edit_message(content=err("That item isn't available anymore."), embed=None, view=None)
            try:
                await member.add_roles(role, reason="Point shop purchase")
            except discord.HTTPException:
                self.db.refund(guild.id, member.id, item)
                return await interaction.response.edit_message(
                    content=err("I couldn't give you the role. You weren't charged."), embed=None, view=None)
        embed = discord.Embed(
            title=f"{I.s('check')} Purchase Successful",
            description=f"You bought {role.mention} for **{item['price']}** points.\n"
                        f"{I.s('wallet')} Balance: **{bal}** points.", color=BRAND)
        await interaction.response.edit_message(embed=embed, view=None)

    # ---------- $pointshop ----------
    @commands.group(name="pointshop", invoke_without_command=True)
    @commands.guild_only()
    async def pointshop(self, ctx: commands.Context):
        """Open the shop. Admins: `$pointshop config`."""
        await self.show_shop(ctx)

    @pointshop.command(name="config")
    @commands.has_permissions(manage_guild=True)
    async def pointshop_config(self, ctx: commands.Context):
        """Add / edit / remove shop roles, prices, stock and emojis."""
        view = ShopConfigView(self, ctx.author)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Shop(bot))
