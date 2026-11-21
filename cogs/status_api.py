"""
Kon status API  —  drop this file into your bot's  cogs/  folder as  cogs/status_api.py

It starts a tiny web server inside the bot and serves live numbers at  GET /status
for the website's Status page and the latency strip on the home page.

Env vars (all optional):
  STATUS_PORT          port to listen on                (default 8095)
  STATUS_ALLOW_ORIGIN  your website URL, for CORS       (default *)
"""
import os
import time

import discord
from aiohttp import web
from discord.ext import commands, tasks

CACHE_SECONDS = 4          # many visitors -> one calculation every few seconds
API_CHECK_SECONDS = 60     # Discord REST round trip is measured once a minute


class StatusAPI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.started = time.time()
        self.runner: web.AppRunner | None = None
        self.api_ms: float | None = None
        self._cache: tuple[float, dict] | None = None

    # ---------- lifecycle ----------
    async def cog_load(self):
        app = web.Application()
        app.router.add_get("/status", self.status)
        app.router.add_route("OPTIONS", "/status", self.preflight)
        app.router.add_get("/", self.status)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        port = int(os.getenv("STATUS_PORT", "8095"))
        await web.TCPSite(self.runner, "0.0.0.0", port).start()
        print(f"[status_api] serving http://0.0.0.0:{port}/status")
        self.measure_api.start()

    async def cog_unload(self):
        self.measure_api.cancel()
        if self.runner:
            await self.runner.cleanup()

    @tasks.loop(seconds=API_CHECK_SECONDS)
    async def measure_api(self):
        if not self.bot.is_ready():
            return
        t0 = time.perf_counter()
        try:
            await self.bot.fetch_user(self.bot.user.id)
            self.api_ms = round((time.perf_counter() - t0) * 1000)
        except discord.HTTPException:
            self.api_ms = None

    @measure_api.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ---------- http ----------
    def headers(self) -> dict:
        return {
            "Access-Control-Allow-Origin": os.getenv("STATUS_ALLOW_ORIGIN", "*"),
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Cache-Control": "no-store",
        }

    async def preflight(self, request: web.Request):
        return web.Response(status=204, headers=self.headers())

    def db_ms(self) -> float | None:
        db = getattr(self.bot, "db", None)
        if db is None or not hasattr(db, "ping"):
            return None
        t0 = time.perf_counter()
        try:
            db.ping()
        except Exception:
            return None
        return round((time.perf_counter() - t0) * 1000, 1)

    def build(self) -> dict:
        b = self.bot
        lat = b.latency
        latency_ms = round(lat * 1000) if lat == lat and lat != float("inf") else 0
        return {
            "online": b.is_ready() and not b.is_closed(),
            "latencyMs": latency_ms,
            "apiMs": self.api_ms,
            "dbMs": self.db_ms(),
            "uptimeSeconds": int(time.time() - self.started),
            "guilds": len(b.guilds),
            "members": sum(g.member_count or 0 for g in b.guilds),
            "channels": sum(len(g.channels) for g in b.guilds),
            "shards": b.shard_count or 1,
            "commands": len(list(b.walk_commands())),
            "updatedAt": int(time.time() * 1000),
        }

    async def status(self, request: web.Request):
        now = time.time()
        if not self._cache or now - self._cache[0] > CACHE_SECONDS:
            self._cache = (now, self.build())
        return web.json_response(self._cache[1], headers=self.headers())


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusAPI(bot))
