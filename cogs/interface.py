"""The 'Voice Channel Control Panel' (persistent buttons + modals + member pickers)."""
import discord
from discord.ext import commands

from core import ratelimit
from core.config import BRAND, PANEL_IMAGE
from core.errors import VCError
from core.icons import I, err, ok

# (icon, action) rows exactly like the panel screenshot (4 x 3)
LAYOUT = [
    [("lock", "lock"), ("unlock", "unlock"), ("hide", "hide"), ("show", "unhide")],
    [("logout", "kick"), ("user_add", "permit"), ("ban", "ban"), ("crown", "transfer")],
    [("edit", "rename"), ("signal", "status"), ("volume", "bitrate"), ("users", "limit")],
]
TOGGLES = {"lock", "unlock", "hide", "unhide"}
PICKERS = {"kick", "permit", "ban", "transfer"}


async def respond(interaction: discord.Interaction, text: str):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


class ValueModal(discord.ui.Modal):
    def __init__(self, svc_call, title, label, placeholder, *, required=True, max_length=100):
        super().__init__(title=title, timeout=300)
        self.svc_call = svc_call
        self.inp = discord.ui.TextInput(label=label, placeholder=placeholder, required=required, max_length=max_length)
        self.add_item(self.inp)

    async def on_submit(self, interaction: discord.Interaction):
        if not await ratelimit.guard_interaction(interaction, "modal"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            msg = ok(await self.svc_call(interaction.user, self.inp.value.strip()))
        except VCError as e:
            msg = err(str(e))
        await interaction.followup.send(msg, ephemeral=True)


class MemberPick(discord.ui.View):
    def __init__(self, svc, invoker: discord.Member, action: str):
        super().__init__(timeout=60)
        self.svc, self.invoker, self.action = svc, invoker, action
        sel = discord.ui.UserSelect(placeholder="Pick a member...", min_values=1, max_values=1)
        sel.callback = self.picked
        self.sel = sel
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker.id

    async def picked(self, interaction: discord.Interaction):
        if not await ratelimit.guard_interaction(interaction, "modal"):
            return
        await interaction.response.defer()
        target = self.sel.values[0]
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)
        if target is None:
            return await interaction.edit_original_response(content=err("They aren't in this server."), view=None)
        try:
            msg = ok(await getattr(self.svc, self.action)(interaction.user, target))
        except VCError as e:
            msg = err(str(e))
        await interaction.edit_original_response(content=msg, view=None)
        self.stop()


class PanelView(discord.ui.View):
    def __init__(self, svc, prefix: str = "kon"):        # prefix "leafy" = buttons of panels posted before the rebrand
        super().__init__(timeout=None)
        self.svc = svc
        for row, items in enumerate(LAYOUT):
            for icon, action in items:
                b = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji=I.e(icon),
                                      custom_id=f"{prefix}:{action}", row=row)
                b.callback = self._make_cb(action)
                self.add_item(b)

    def _make_cb(self, action: str):
        async def cb(interaction: discord.Interaction):
            await self.handle(interaction, action)
        return cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ratelimit.guard_interaction(interaction, "button")

    async def handle(self, interaction: discord.Interaction, action: str):
        member = interaction.user
        svc = self.svc
        try:
            svc.owned(member)  # "You must be in your custom VC to manage it."
            if action in TOGGLES:
                await interaction.response.defer(ephemeral=True)
                await getattr(svc, action)(member)
                return await interaction.followup.send(ok("Done."), ephemeral=True)
            if action in PICKERS:
                return await interaction.response.send_message(
                    f"Who do you want to **{action}**?", view=MemberPick(svc, member, action), ephemeral=True)
            if action == "rename":
                return await interaction.response.send_modal(
                    ValueModal(svc.rename, "Rename VC", "New name", "e.g. chill zone", max_length=90))
            if action == "status":
                return await interaction.response.send_modal(
                    ValueModal(svc.status, "VC Status", "Status text (empty = clear)", "e.g. watching a movie",
                               required=False, max_length=500))
            if action == "limit":
                async def _limit(m, v):
                    return await svc.limit(m, _to_int(v, "limit"))
                return await interaction.response.send_modal(
                    ValueModal(_limit, "Set Limit", "User limit (0 = unlimited)", "0 - 99", max_length=2))
            if action == "bitrate":
                top = interaction.guild.bitrate_limit // 1000

                async def _bitrate(m, v):
                    return await svc.bitrate(m, _to_int(v, "bitrate"))
                return await interaction.response.send_modal(
                    ValueModal(_bitrate, "Bitrate", f"Bitrate in kbps (8 - {int(top)})", "64", max_length=3))
        except VCError as e:
            await respond(interaction, err(str(e)))


def _to_int(v: str, what: str) -> int:
    try:
        return int(v)
    except ValueError:
        raise VCError(f"Enter a number for the {what}.")


def panel_embed() -> discord.Embed:
    e = discord.Embed(title="Voice Channel Control Panel", color=BRAND)
    e.set_image(url="attachment://panel.png")
    return e


class Interface(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    async def post_panel(self, channel: discord.TextChannel) -> discord.Message:
        """Send a fresh panel and remember it; the old one (if any) is deleted."""
        g = self.store.guild(channel.guild.id)
        old_ch, old_msg = g.get("interface"), g.get("panel_msg")
        if old_ch and old_msg:
            oc = self.bot.get_channel(old_ch)
            if oc:
                try:
                    await oc.get_partial_message(old_msg).delete()
                except discord.HTTPException:
                    pass
        msg = await channel.send(
            embed=panel_embed(), file=discord.File(PANEL_IMAGE, filename="panel.png"), view=PanelView(self.bot.vc_service))
        g["interface"], g["panel_msg"] = channel.id, msg.id
        self.store.save_soon()
        return msg


async def setup(bot):
    await bot.add_cog(Interface(bot))
