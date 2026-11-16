"""Custom (join-to-create) voice channels: service + commands + events."""
import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.http import Route

from core import ratelimit
from core.config import BLURPLE, VC_PREFIX
from core.errors import VCError
from core.icons import I, ok

log = logging.getLogger("leafy.vc")

NOT_IN_VC = "You must be in your custom VC to manage it."


def parse_duration(text: str) -> int:
    """'10m', '1h30m', '45s', '2d' -> seconds (max 24h). A bare number = minutes."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text) * 60
    parts = re.findall(r"(\d+)\s*([smhd])", text)
    if not parts or re.sub(r"(\d+)\s*([smhd])", "", text).strip():
        raise VCError("Invalid duration. Use something like `30s`, `10m`, `2h`.")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    secs = sum(int(n) * mult[u] for n, u in parts)
    if secs < 5 or secs > 86400:
        raise VCError("Duration must be between 5 seconds and 24 hours.")
    return secs


def fmt_secs(s: int) -> str:
    out = []
    for n, u in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        q, s = divmod(s, n)
        if q:
            out.append(f"{q}{u}")
    return " ".join(out) or "0s"


class VCService:
    """All VC actions. Used by prefix/slash commands AND the interface buttons."""

    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self._creating: set[int] = set()

    # ---------- helpers ----------
    def owned(self, member: discord.Member):
        vs = member.voice
        if not vs or not vs.channel:
            raise VCError(NOT_IN_VC)
        rec = self.store.vc(vs.channel.id)
        if not rec or rec["owner"] != member.id:
            raise VCError(NOT_IN_VC)
        return vs.channel, rec

    async def _ov(self, ch, target, reason: str, **perms):
        ov = ch.overwrites_for(target)
        ov.update(**perms)
        await ch.set_permissions(target, overwrite=None if ov.is_empty() else ov, reason=reason)

    @staticmethod
    def _http(e: discord.HTTPException) -> VCError:
        if isinstance(e, discord.Forbidden):
            return VCError("I don't have permission to do that. Please check my role / channel permissions.")
        return VCError(f"Discord error: {e.text or e.status}")

    # ---------- lock / hide ----------
    async def lock(self, member):
        ch, _ = self.owned(member)
        try:
            await self._ov(ch, ch.guild.default_role, "VC locked", connect=False)
        except discord.HTTPException as e:
            raise self._http(e)
        return "Locked your voice channel."

    async def unlock(self, member):
        ch, _ = self.owned(member)
        try:
            await self._ov(ch, ch.guild.default_role, "VC unlocked", connect=None)
        except discord.HTTPException as e:
            raise self._http(e)
        return "Unlocked your voice channel."

    async def hide(self, member):
        ch, _ = self.owned(member)
        try:
            await self._ov(ch, ch.guild.default_role, "VC hidden", view_channel=False)
        except discord.HTTPException as e:
            raise self._http(e)
        return "Your voice channel is now hidden."

    async def unhide(self, member):
        ch, _ = self.owned(member)
        try:
            await self._ov(ch, ch.guild.default_role, "VC unhidden", view_channel=None)
        except discord.HTTPException as e:
            raise self._http(e)
        return "Your voice channel is now visible."

    # ---------- member actions ----------
    async def kick(self, member, target: discord.Member):
        ch, _ = self.owned(member)
        if target.id == member.id:
            raise VCError("You can't kick yourself.")
        if target not in ch.members:
            raise VCError("That user is not in your VC.")
        try:
            await target.move_to(None, reason=f"VC kick by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"**{target.display_name}** has been kicked from the session."

    async def ban(self, member, target: discord.Member):
        ch, rec = self.owned(member)
        if target.id == member.id:
            raise VCError("You can't ban yourself.")
        if target.bot:
            raise VCError("You can't ban a bot.")
        try:
            await self._ov(ch, target, f"VC ban by {member}", connect=False, view_channel=False)
            if target.id not in rec["banned"]:
                rec["banned"].append(target.id)
                self.store.save_soon()
            if target in ch.members:
                await target.move_to(None, reason=f"VC ban by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"**{target.display_name}** has been banned and removed from the session."

    async def unban(self, member, target: discord.abc.User):
        ch, rec = self.owned(member)
        if target.id not in rec["banned"]:
            raise VCError("That user is not banned from your VC.")
        try:
            await self._ov(ch, discord.Object(id=target.id), f"VC unban by {member}", connect=None, view_channel=None)
        except discord.HTTPException as e:
            raise self._http(e)
        rec["banned"].remove(target.id)
        self.store.save_soon()
        return f"**{getattr(target, 'display_name', target.name)}** has been unbanned."

    async def permit(self, member, target: discord.Member):
        ch, rec = self.owned(member)
        try:
            await self._ov(ch, target, f"VC permit by {member}", connect=True, view_channel=True)
        except discord.HTTPException as e:
            raise self._http(e)
        if target.id in rec["banned"]:
            rec["banned"].remove(target.id)
            self.store.save_soon()
        return f"**{target.display_name}** can now join your voice channel."

    async def permit_all(self, member):
        ch, rec = self.owned(member)
        targets = [m for m in ch.members if m.id != member.id]
        if not targets:
            raise VCError("There is nobody else in your VC to permit.")
        try:
            for m in targets:
                await self._ov(ch, m, f"VC permit all by {member}", connect=True, view_channel=True)
                if m.id in rec["banned"]:
                    rec["banned"].remove(m.id)
                await asyncio.sleep(0.25)
        except discord.HTTPException as e:
            raise self._http(e)
        self.store.save_soon()
        return f"Permitted **{len(targets)}** member(s) in your voice channel."

    async def transfer(self, member, target: discord.Member):
        ch, rec = self.owned(member)
        if target.id == member.id:
            raise VCError("You already own this VC.")
        if target.bot:
            raise VCError("You can't transfer ownership to a bot.")
        if target not in ch.members:
            raise VCError("That user is not in your VC.")
        await self.set_owner(ch, rec, target, old=member)
        return f"Ownership transferred to **{target.display_name}**."

    async def set_owner(self, ch, rec, new: discord.Member, old=None):
        try:
            await ch.set_permissions(new, overwrite=discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True), reason="VC owner changed")
            if old is not None:
                await ch.set_permissions(old, overwrite=None, reason="VC owner changed")
        except discord.HTTPException as e:
            raise self._http(e)
        rec["owner"] = new.id
        if new.id in rec["banned"]:
            rec["banned"].remove(new.id)
        self.store.save_soon()

    # ---------- channel settings ----------
    async def limit(self, member, n: int):
        ch, _ = self.owned(member)
        if not 0 <= n <= 99:
            raise VCError("The limit must be between **0** (unlimited) and **99**.")
        try:
            await ch.edit(user_limit=n, reason=f"VC limit by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return "Removed the user limit." if n == 0 else f"User limit set to **{n}**."

    async def rename(self, member, name: str):
        ch, _ = self.owned(member)
        new = name.strip()[:100]          # exactly what the owner typed - no automatic prefix
        if not new:
            raise VCError("Please give a valid name.")
        ra = ratelimit.check("rename", ch.id)
        if ra:
            raise VCError(f"Discord only allows 2 renames per 10 minutes. Try again in **{ratelimit.fmt(ra)}**.")
        try:
            await ch.edit(name=new, reason=f"VC rename by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"Voice channel renamed to **{new}**."

    async def status(self, member, text: str):
        ch, _ = self.owned(member)
        text = text.strip()[:500]
        try:
            await self.bot.http.request(
                Route("PUT", "/channels/{channel_id}/voice-status", channel_id=ch.id),
                json={"status": text or None},
            )
        except discord.Forbidden:
            raise VCError("I need the **Set Voice Channel Status** permission for that.")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"VC status updated to: **{text}**" if text else "VC status cleared."

    async def bitrate(self, member, kbps: int):
        ch, _ = self.owned(member)
        top = ch.guild.bitrate_limit // 1000
        if not 8 <= kbps <= top:
            raise VCError(f"Bitrate must be between **8** and **{int(top)}** kbps for this server.")
        try:
            await ch.edit(bitrate=kbps * 1000, reason=f"VC bitrate by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"Bitrate set to **{kbps} kbps**."

    async def mute(self, member, target: discord.Member, seconds: int):
        ch, _ = self.owned(member)
        if target.id == member.id:
            raise VCError("You can't mute yourself.")
        if target not in ch.members:
            raise VCError("That user is not in your VC.")
        try:
            await target.edit(mute=True, reason=f"VC mute by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        self.store.data["mutes"][f"{ch.guild.id}:{target.id}"] = discord.utils.utcnow().timestamp() + seconds
        self.store.save_soon()
        return f"**{target.display_name}** has been server-muted for **{fmt_secs(seconds)}**."

    async def toggle_drags(self, member):
        _, rec = self.owned(member)
        rec["block_drags"] = not rec.get("block_drags", False)
        self.store.save_soon()
        return ("Drag requests are now **blocked** for your VC." if rec["block_drags"]
                else "Drag requests are now **allowed** for your VC.")

    # ---------- create / cleanup ----------
    async def create_for(self, member: discord.Member, create_ch: discord.VoiceChannel):
        if member.id in self._creating:
            return
        ra = ratelimit.check("create_vc", member.id)
        if ra:
            log.info("create_vc rate limited for %s", member)
            try:
                await member.move_to(None, reason="Creating VCs too fast")
            except discord.HTTPException:
                pass
            return
        self._creating.add(member.id)
        try:
            guild = member.guild
            cat = create_ch.category
            overwrites = dict(cat.overwrites) if cat else {}
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True)
            kwargs = dict(
                name=f"{VC_PREFIX}{member.name}"[:100],
                category=cat,
                user_limit=0,                        # no user limit on new custom VCs
                bitrate=min(create_ch.bitrate, guild.bitrate_limit),
                reason=f"Custom VC for {member}",
            )
            try:
                me_ov = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True, move_members=True)
                ch = await guild.create_voice_channel(overwrites={**overwrites, guild.me: me_ov}, **kwargs)
            except discord.Forbidden:
                try:
                    ch = await guild.create_voice_channel(overwrites=overwrites, **kwargs)
                except discord.HTTPException as e:
                    log.warning("could not create VC in %s: %s", guild, e)
                    return
            except discord.HTTPException as e:
                log.warning("could not create VC in %s: %s", guild, e)
                return
            self.store.add_vc(ch.id, guild.id, member.id)
            try:
                await member.move_to(ch)
            except discord.HTTPException:
                self.store.remove_vc(ch.id)
                try:
                    await ch.delete(reason="Owner left before being moved")
                except discord.HTTPException:
                    pass
        finally:
            self._creating.discard(member.id)

    async def on_leave(self, member, ch: discord.VoiceChannel, rec: dict):
        humans = [m for m in ch.members if not m.bot]
        if not humans:
            self.store.remove_vc(ch.id)
            try:
                await ch.delete(reason="Custom VC empty")
            except discord.HTTPException:
                pass
            return
        if member.id == rec["owner"]:
            new = humans[0]
            try:
                await self.set_owner(ch, rec, new, old=member)
                emb = discord.Embed(
                    description=f"{I.s('crown')} {new.mention} is now the owner of this VC.", color=BLURPLE)
                await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
            except (VCError, discord.HTTPException):
                pass


class VC(commands.Cog):
    """Custom VC commands (hybrid = work as $prefix AND /slash)."""

    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.svc = bot.vc_service
        self._ready_done = False

    async def cog_load(self):
        self.mute_loop.start()

    async def cog_unload(self):
        self.mute_loop.cancel()

    # ---------- plumbing ----------
    async def _run(self, ctx: commands.Context, op, *args):
        await ctx.defer()
        msg = await op(ctx.author, *args)
        await ctx.reply(ok(msg), mention_author=False)

    # ---------- events ----------
    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready_done:
            return
        self._ready_done = True
        for cid, rec in list(self.store.data["vcs"].items()):
            ch = self.bot.get_channel(int(cid))
            if ch is None:
                self.store.remove_vc(int(cid))
            elif not [m for m in ch.members if not m.bot]:
                self.store.remove_vc(int(cid))
                try:
                    await ch.delete(reason="Custom VC empty (startup cleanup)")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        if after.channel:
            g = self.store.guild(member.guild.id)
            if after.channel.id == g.get("create"):
                await self.svc.create_for(member, after.channel)
            else:
                rec = self.store.vc(after.channel.id)
                if rec and member.id in rec["banned"] and member.id != rec["owner"]:
                    try:
                        await member.move_to(None, reason="Banned from this custom VC")
                    except discord.HTTPException:
                        pass
        if before.channel:
            rec = self.store.vc(before.channel.id)
            if rec:
                await self.svc.on_leave(member, before.channel, rec)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        self.store.remove_vc(channel.id)
        g = self.store.data["guilds"].get(str(channel.guild.id), {})
        changed = False
        for k in ("create", "interface"):
            if g.get(k) == channel.id:
                g.pop(k, None)
                if k == "interface":
                    g.pop("panel_msg", None)
                changed = True
        if changed:
            self.store.save_soon()

    @tasks.loop(seconds=15)
    async def mute_loop(self):
        now = discord.utils.utcnow().timestamp()
        for key, exp in list(self.store.data["mutes"].items()):
            if exp > now:
                continue
            self.store.data["mutes"].pop(key, None)
            self.store.save_soon()
            gid, uid = map(int, key.split(":"))
            guild = self.bot.get_guild(gid)
            m = guild.get_member(uid) if guild else None
            if m and m.voice and m.voice.mute:
                try:
                    await m.edit(mute=False, reason="VC mute expired")
                except discord.HTTPException:
                    pass

    @mute_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ---------- info ----------
    @commands.hybrid_command(name="vcinfo", aliases=['vci'], description="View the current owner and details of this voice channel")
    @commands.guild_only()
    async def vcinfo(self, ctx: commands.Context):
        ch = None
        v = ctx.author.voice
        if v and v.channel and self.store.vc(v.channel.id):
            ch = v.channel
        elif isinstance(ctx.channel, discord.VoiceChannel) and self.store.vc(ctx.channel.id):
            ch = ctx.channel
        if ch is None:
            raise VCError("This isn't a custom voice channel.")
        rec = self.store.vc(ch.id)
        owner = ctx.guild.get_member(rec["owner"])
        owner_name = owner.name if owner else "Unknown"
        embed = discord.Embed(
            description=(
                f"{I.s('speaker')} **Channel:** `{ch.name}`\n"
                f"{I.s('crown')} **Owner:** {owner_name} (`{rec['owner']}`)\n"
                f"{I.s('users')} **Members:** `{len(ch.members)}`"
            ),
            color=BLURPLE,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Voice Channel Information",
                         icon_url=(ctx.guild.icon.url if ctx.guild.icon else self.bot.user.display_avatar.url))
        if owner:
            embed.set_thumbnail(url=owner.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    # ---------- lock / hide ----------
    @commands.hybrid_command(name="vclock", aliases=['lock', 'vclk'], description="Lock session")
    @commands.guild_only()
    async def vclock(self, ctx):
        await self._run(ctx, self.svc.lock)

    @commands.hybrid_command(name="vcunlock", aliases=['unlock', 'vcul'], description="Unlock session")
    @commands.guild_only()
    async def vcunlock(self, ctx):
        await self._run(ctx, self.svc.unlock)

    @commands.hybrid_command(name="vchide", aliases=['hide'], description="Hide session")
    @commands.guild_only()
    async def vchide(self, ctx):
        await self._run(ctx, self.svc.hide)

    @commands.hybrid_command(name="vcunhide", aliases=['unhide', 'vcshow'], description="Unhide session")
    @commands.guild_only()
    async def vcunhide(self, ctx):
        await self._run(ctx, self.svc.unhide)

    # ---------- member actions ----------
    @commands.hybrid_command(name="vckick", aliases=['kick', 'vck'], description="Kick user")
    @app_commands.describe(user="Member to kick from your VC")
    @commands.guild_only()
    async def vckick(self, ctx, user: discord.Member):
        await self._run(ctx, self.svc.kick, user)

    @commands.hybrid_command(name="vcban", aliases=['vcb'], description="Ban user")
    @app_commands.describe(user="Member to ban from your VC")
    @commands.guild_only()
    async def vcban(self, ctx, user: discord.Member):
        await self._run(ctx, self.svc.ban, user)

    @commands.hybrid_command(name="vcunban", aliases=['vcub'], description="Unban user")
    @app_commands.describe(user="User to unban from your VC")
    @commands.guild_only()
    async def vcunban(self, ctx, user: discord.User):
        await self._run(ctx, self.svc.unban, user)

    @commands.hybrid_command(name="vcpermit", aliases=['permit', 'allow', 'vcp'], description="Permit user")
    @app_commands.describe(user="Member allowed to join your locked/hidden VC")
    @commands.guild_only()
    async def vcpermit(self, ctx, user: discord.Member):
        await self._run(ctx, self.svc.permit, user)

    @commands.hybrid_command(name="vcpermitall", aliases=['permitall', 'vcpa'], description="Permit all current users in your custom VC")
    @commands.guild_only()
    async def vcpermitall(self, ctx):
        await self._run(ctx, self.svc.permit_all)

    @commands.hybrid_command(name="vctransfer", aliases=['vct'], description="Transfer ownership")
    @app_commands.describe(user="New owner (must be in your VC)")
    @commands.guild_only()
    async def vctransfer(self, ctx, user: Optional[discord.Member] = None):
        if user is None and ctx.interaction is None and ctx.message.reference:      # "$vctransfer" as a reply
            ref = ctx.message.reference
            msg = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if msg is None and ref.message_id:
                try:
                    msg = await ctx.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    msg = None
            if msg is not None:
                user = ctx.guild.get_member(msg.author.id)
        if user is None:
            raise VCError("Reply to the user's message or mention them: `$vctransfer @user`.")
        await self._run(ctx, self.svc.transfer, user)

    @commands.hybrid_command(name="vcmute", aliases=['vcm'], description="Server-mute user for a duration")
    @app_commands.describe(user="Member to mute", duration="e.g. 30s, 10m, 2h (max 24h)")
    @commands.guild_only()
    async def vcmute(self, ctx, user: discord.Member, duration: str = "10m"):
        await self._run(ctx, self.svc.mute, user, parse_duration(duration))

    # ---------- settings ----------
    @commands.hybrid_command(name="vclimit", aliases=['limit', 'vclim'], description="Set capacity")
    @app_commands.describe(limit="0 = unlimited, max 99")
    @commands.guild_only()
    async def vclimit(self, ctx, limit: commands.Range[int, 0, 99]):
        await self._run(ctx, self.svc.limit, limit)

    @commands.hybrid_command(name="vcrename", aliases=['rename', 'vcn', 'vcname'], description="Update VC name")
    @app_commands.describe(name="New channel name")
    @commands.guild_only()
    async def vcrename(self, ctx, *, name: str):
        await self._run(ctx, self.svc.rename, name)

    @commands.hybrid_command(name="vcstatus", aliases=['vcs'], description="Set voice status")
    @app_commands.describe(text="Status text (leave empty to clear)")
    @commands.guild_only()
    async def vcstatus(self, ctx, *, text: str = ""):
        await self._run(ctx, self.svc.status, text)

    @commands.hybrid_command(name="vcbitrate", aliases=['bitrate', 'vcbr'], description="Change quality")
    @app_commands.describe(kbps="Bitrate in kbps (8 - server max)")
    @commands.guild_only()
    async def vcbitrate(self, ctx, kbps: commands.Range[int, 8, 384]):
        await self._run(ctx, self.svc.bitrate, kbps)

    @commands.hybrid_command(name="denydrags", aliases=['denydrag', 'dd'], description="VC Owner: Toggle privacy to block drag requests for your VC")
    @commands.guild_only()
    async def denydrags(self, ctx):
        await self._run(ctx, self.svc.toggle_drags)


async def setup(bot):
    await bot.add_cog(VC(bot))
