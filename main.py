import asyncio
import itertools
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ------------------------------------------------------------
# Furious - Discord music bot
# - Slash commands (/play) and mention-prefix commands (@Furious play)
# - Ping the bot (just @Furious) -> stats embed + Trust button
# - 24/7 mode, rotating status, trusted counter
# - YouTube cookies fix + SoundCloud fallback
# ------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "@Furious "  # display only; the real prefix is the bot mention
DEV_NAME = "shubh"
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
TRUSTED_FILE = BASE_DIR / "trusted.json"

# ---- YouTube cookies ----
# Option 1: put a cookies.txt file next to this script (used first if it exists)
# Option 2: read cookies from a browser. Set COOKIE_BROWSER in .env
#           (firefox, chrome, edge, brave, opera, vivaldi) or "none" to disable.
COOKIES_FILE = BASE_DIR / "cookies.txt"
COOKIE_BROWSER = os.getenv("COOKIE_BROWSER", "firefox").strip().lower()

FFMPEG_PATH = shutil.which("ffmpeg")
MAX_DURATION = 20 * 60
IDLE_DISCONNECT_SECONDS = 180
EMBED_COLOR = discord.Color.from_rgb(24, 27, 38)

# ---- Custom bot emojis ----
EMOJI_KNIFE = "<a:404017yellowpixelknife:1538479409340743680>"
EMOJI_SKIP = "<:22838skip:1537702524218511452>"
EMOJI_PAUSE = "<:776450pause:1537702507210612786>"
EMOJI_TICK = "<:763305tick:1537700918722691133>"
EMOJI_ERROR = "<a:880726error:1537700477955735622>"
EMOJI_SPOTIFY = "<:214004pixelspotify:1537699774596386926>"

# True  = delete the previous "Now Playing" message when a new one is sent
# False = keep it, but remove its buttons
DELETE_OLD_PANEL = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("Furious")

players: dict[int, "MusicPlayer"] = {}


# ------------------------------------------------------------
# yt-dlp option builders (with cookies)
# ------------------------------------------------------------
def cookie_options() -> dict:
    if COOKIES_FILE.exists():
        return {"cookiefile": str(COOKIES_FILE)}
    if COOKIE_BROWSER and COOKIE_BROWSER != "none":
        return {"cookiesfrombrowser": (COOKIE_BROWSER,)}
    return {}


def base_options(use_cookies: bool = True) -> dict:
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "source_address": "0.0.0.0",
        "remote_components": ["ejs:github"],
    }
    if use_cookies:
        opts.update(cookie_options())
    return opts


# ------------------------------------------------------------
# Trusted users storage
# ------------------------------------------------------------
def load_trusted() -> set[int]:
    if TRUSTED_FILE.exists():
        try:
            with open(TRUSTED_FILE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            log.exception("Could not read trusted.json, starting empty.")
    return set()


def save_trusted(data: set[int]) -> None:
    try:
        with open(TRUSTED_FILE, "w") as f:
            json.dump(list(data), f)
    except OSError:
        log.exception("Could not save trusted.json")


trusted_users: set[int] = load_trusted()


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def get_uptime() -> str:
    s = int(time.time() - bot.start_time)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "LIVE"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def clean_downloads() -> None:
    for file in DOWNLOAD_DIR.iterdir():
        if file.is_file():
            try:
                file.unlink()
            except OSError:
                log.warning("Could not remove old download: %s", file)


def startup_check() -> None:
    log.info("========== FURIOUS STARTUP ==========")
    log.info("discord.py: %s", discord.__version__)
    log.info("yt-dlp: %s", yt_dlp.version.__version__)
    log.info("Python: %s", sys.version.split()[0])
    log.info("FFmpeg: %s", FFMPEG_PATH or "NOT FOUND")
    log.info("Opus loaded: %s (not required for FFmpegOpusAudio)", discord.opus.is_loaded())
    log.info("Deno: %s", shutil.which("deno") or "not found")
    if COOKIES_FILE.exists():
        log.info("Cookies: using cookies.txt")
    elif COOKIE_BROWSER and COOKIE_BROWSER != "none":
        log.info("Cookies: reading from browser '%s'", COOKIE_BROWSER)
    else:
        log.info("Cookies: disabled")
    if FFMPEG_PATH:
        try:
            result = subprocess.run(
                [FFMPEG_PATH, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            log.info("FFmpeg check: %s", "OK" if result.returncode == 0 else "FAILED")
        except Exception:
            log.exception("FFmpeg check failed")
    log.info("=====================================")


# ------------------------------------------------------------
# Extract (search) - with cookie retry + SoundCloud fallback
# ------------------------------------------------------------
def _extract_with(query: str, use_cookies: bool, search: bool) -> dict | None:
    opts = base_options(use_cookies)
    if search:
        opts["default_search"] = "ytsearch1"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)

    if info and "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)
    return info


def _extract_sync(query: str) -> dict | None:
    info = None
    is_url = query.startswith(("http://", "https://"))

    # 1) YouTube with cookies -> 2) YouTube without cookies -> 3) SoundCloud
    attempts = [
        (query, True),
        (query, False),
    ]
    if not is_url:
        attempts.append((f"scsearch1:{query}", False))

    last_error: Exception | None = None
    for target, use_cookies in attempts:
        try:
            info = _extract_with(target, use_cookies, search=not target.startswith("scsearch"))
            if info:
                break
        except Exception as error:
            last_error = error
            log.warning("Extract attempt failed (%s, cookies=%s): %s", target[:40], use_cookies, str(error)[:150])
    else:
        if last_error:
            raise last_error
        return None

    if not info:
        return None

    duration = info.get("duration")
    if duration and duration > MAX_DURATION:
        raise ValueError("Tracks longer than 20 minutes are not supported.")

    webpage = info.get("webpage_url") or info.get("original_url") or query
    return {
        "title": info.get("title") or "Unknown title",
        "webpage": webpage,
        "thumbnail": info.get("thumbnail"),
        "duration": duration,
        "requester": "Unknown",
        "path": None,
    }


async def extract_song(query: str) -> dict | None:
    return await asyncio.to_thread(_extract_sync, query)


# ------------------------------------------------------------
# Download - with cookie retry + SoundCloud fallback
# ------------------------------------------------------------
def _download_target(target: str, guild_id: int, use_cookies: bool) -> str:
    opts = base_options(use_cookies)
    opts["outtmpl"] = str(DOWNLOAD_DIR / f"{guild_id}_%(id)s.%(ext)s")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=True)
        if info and "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise RuntimeError("yt-dlp returned no audio information.")
        return ydl.prepare_filename(info)


def _download_sync(webpage: str, title: str, guild_id: int) -> str:
    attempts = [
        (webpage, True),                      # YouTube + cookies
        (webpage, False),                     # YouTube, no cookies
        (f"scsearch1:{title}", False),        # SoundCloud fallback
    ]
    last_error: Exception | None = None
    for target, use_cookies in attempts:
        try:
            path = _download_target(target, guild_id, use_cookies)
            if Path(path).is_file():
                if target.startswith("scsearch"):
                    log.info("Used SoundCloud fallback for: %s", title)
                return path
        except Exception as error:
            last_error = error
            log.warning("Download attempt failed (%s, cookies=%s): %s", target[:40], use_cookies, str(error)[:150])
    raise RuntimeError(str(last_error) if last_error else "All download attempts failed.")


async def download_song(song: dict, guild_id: int) -> str:
    path = await asyncio.to_thread(_download_sync, song["webpage"], song["title"], guild_id)
    if not Path(path).is_file():
        raise FileNotFoundError("The downloaded audio file was not found.")
    song["path"] = path
    return path


def remove_song_file(song: dict | None) -> None:
    if not song:
        return
    path = song.get("path")
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove audio file: %s", path)
        song["path"] = None


def volume_bar(volume: float, size: int = 10) -> str:
    filled = max(0, min(size, round(volume * size)))
    return "▰" * filled + "▱" * (size - filled)


# ------------------------------------------------------------
# Player embed
# ------------------------------------------------------------
def make_player_embed(player: "MusicPlayer", status: str = "Playing") -> discord.Embed:
    song = player.current
    mode_247 = "On" if player.always_on else "Off"

    # ---------- Idle panel ----------
    if not song:
        embed = discord.Embed(
            description=(
                "### 💤  Nothing playing\n"
                f"Join a voice channel and use **/play** or **{PREFIX}play** to start the vibe.\n"
                "▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱"
            ),
            color=discord.Color.from_rgb(88, 94, 120),
        )
        embed.set_author(name="FURIOUS  •  Music Player")
        embed.add_field(name="🎧  Status", value="`Idle`", inline=True)
        embed.add_field(name="🎶  Queue", value=f"`{len(player.queue)} tracks`", inline=True)
        embed.add_field(name="🔊  Volume", value=f"`{volume_bar(player.volume)}`", inline=True)
        embed.add_field(name="♾️  24/7", value=f"`{mode_247}`", inline=True)
        embed.set_footer(text=f"Furious Audio  •  Dev: {DEV_NAME}")
        return embed

    # ---------- Now playing panel ----------
    paused = status.lower() == "paused"
    icon = EMOJI_PAUSE if paused else "▶️"
    color = (
        discord.Color.from_rgb(255, 184, 77)       # amber when paused
        if paused
        else discord.Color.from_rgb(138, 99, 255)  # violet when playing
    )

    title = discord.utils.escape_markdown(song["title"])[:200]
    url = song["webpage"]
    duration = format_duration(song.get("duration"))
    vol_percent = round(player.volume * 100)

    embed = discord.Embed(
        description=(
            f"### {EMOJI_SPOTIFY} [{title}]({url})\n"
            f"{icon} **{status.title()}**   •   ⏱️ `{duration}`   •   🔁 `{'On' if player.loop else 'Off'}`   •   ♾️ `{mode_247}`\n\n"
            f"🔊 `{volume_bar(player.volume)}` **{vol_percent}%**"
        ),
        color=color,
    )
    embed.set_author(name="FURIOUS  •  Now Playing")

    if song.get("thumbnail"):
        embed.set_image(url=song["thumbnail"])

    requester = discord.utils.escape_markdown(song.get("requester", "Unknown"))[:100]
    embed.add_field(name=f"{EMOJI_KNIFE}  Requested by", value=requester, inline=True)
    embed.add_field(name="🎶  In queue", value=f"`{len(player.queue)} tracks`", inline=True)

    if player.queue:
        nxt = player.queue[0]
        next_title = discord.utils.escape_markdown(nxt["title"])[:150]
        embed.add_field(
            name=f"{EMOJI_SKIP}  Up next",
            value=f"[{next_title}]({nxt['webpage']})",
            inline=False,
        )

    embed.set_footer(text="Furious Audio  •  Use the controls below")
    return embed


# ------------------------------------------------------------
# Music player
# ------------------------------------------------------------
class MusicPlayer:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.loop = False
        self.skipped = False
        self.volume = 0.50
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.idle_task: asyncio.Task | None = None
        self.voice: discord.VoiceClient | None = None
        self.text_channel = None
        self.panel_message: discord.Message | None = None
        self.status = "Playing"
        # 24/7 mode
        self.always_on = False
        self.voice_channel_id: int | None = None

    def is_active(self) -> bool:
        return bool(self.voice and (self.voice.is_playing() or self.voice.is_paused()))

    async def send(self, content=None, **kwargs):
        if self.text_channel:
            try:
                return await self.text_channel.send(content, **kwargs)
            except (discord.HTTPException, discord.Forbidden):
                log.exception("Could not send message to the music text channel.")
        return None

    async def update_panel(self):
        """Edit the existing panel (pause / resume / loop / idle changes)."""
        if not self.panel_message:
            return
        try:
            await self.panel_message.edit(
                embed=make_player_embed(self, self.status),
                view=PlayerView(self.guild_id),
            )
        except discord.NotFound:
            self.panel_message = None
        except discord.HTTPException:
            log.exception("Could not update player panel.")

    async def publish_panel(self):
        """Send a NEW now-playing embed and retire the old one."""
        old = self.panel_message
        new = await self.send(
            embed=make_player_embed(self, self.status),
            view=PlayerView(self.guild_id),
        )
        if not new:
            return
        self.panel_message = new

        if old:
            try:
                if DELETE_OLD_PANEL:
                    await old.delete()
                else:
                    await old.edit(view=None)
            except discord.HTTPException:
                pass

    async def add(self, song: dict):
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()

        async with self.lock:
            self.queue.append(song)

        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.play_next())

    def skip(self):
        self.skipped = True
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()

    async def stop(self):
        self.queue.clear()
        self.loop = False
        self.skipped = True
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()
        else:
            remove_song_file(self.current)
            self.current = None
            self.status = "Idle"
            await self.update_panel()

    def schedule_idle_disconnect(self):
        if self.always_on:
            return  # 24/7 mode: never leave
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = asyncio.create_task(self.idle_disconnect())

    async def idle_disconnect(self):
        try:
            await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
            if self.always_on:
                return
            if self.voice and self.voice.is_connected() and not self.is_active() and not self.queue:
                await self.voice.disconnect()
                self.voice = None
        except asyncio.CancelledError:
            pass
        except discord.HTTPException:
            log.exception("Idle disconnect failed.")

    async def play_next(self):
        event_loop = asyncio.get_running_loop()

        while True:
            repeat = False
            async with self.lock:
                if self.loop and self.current and not self.skipped:
                    song = self.current
                    repeat = True
                elif self.queue:
                    song = self.queue.pop(0)
                    old_song = self.current
                    self.current = song
                    if old_song is not song:
                        remove_song_file(old_song)
                else:
                    remove_song_file(self.current)
                    self.current = None
                    self.skipped = False
                    self.status = "Idle"
                    await self.update_panel()
                    self.schedule_idle_disconnect()
                    return
                self.skipped = False

            if not self.voice or not self.voice.is_connected():
                remove_song_file(song)
                self.current = None
                self.status = "Idle"
                await self.update_panel()
                return

            try:
                if not song.get("path") or not Path(song["path"]).is_file():
                    log.info("[%s] Downloading %s", self.guild_id, song["title"])
                    await download_song(song, self.guild_id)
            except Exception as error:
                log.exception("Download failed.")
                await self.send(f"{EMOJI_ERROR} Couldn't load **{song['title']}**: `{str(error)[:250]}`")
                remove_song_file(song)
                self.current = None
                continue

            try:
                if not FFMPEG_PATH:
                    raise RuntimeError("FFmpeg is not installed or is not on PATH.")

                source = discord.FFmpegOpusAudio(
                    song["path"],
                    executable=FFMPEG_PATH,
                    options=f'-vn -af "volume={self.volume}" -loglevel warning',
                )

                finished = asyncio.Event()
                started_at = time.monotonic()

                def after(error):
                    if error:
                        log.error("Voice playback error: %s", error)
                    event_loop.call_soon_threadsafe(finished.set)

                self.voice.play(source, after=after)
                self.status = "Playing"

                # New track -> brand new embed. A loop repeat just refreshes the panel.
                if repeat and self.panel_message:
                    await self.update_panel()
                else:
                    await self.publish_panel()

                log.info("[%s] Playing %s", self.guild_id, song["title"])
                await finished.wait()

                elapsed = time.monotonic() - started_at
                log.info("[%s] Track ended after %.1fs", self.guild_id, elapsed)

            except Exception as error:
                log.exception("Playback failed.")
                await self.send(f"{EMOJI_ERROR} Couldn't play **{song['title']}**: `{str(error)[:250]}`")

            was_skipped = self.skipped
            if not self.loop or was_skipped:
                remove_song_file(song)

            if was_skipped:
                self.skipped = False


def get_player(guild_id: int) -> MusicPlayer:
    if guild_id not in players:
        players[guild_id] = MusicPlayer(guild_id)
    return players[guild_id]


def user_in_bot_channel(user, player: MusicPlayer) -> bool:
    user_voice = getattr(user, "voice", None)
    if not user_voice or not user_voice.channel:
        return False
    return bool(player.voice and player.voice.channel == user_voice.channel)


# ------------------------------------------------------------
# Player buttons
# ------------------------------------------------------------
class PlayerView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=3 * 60 * 60)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        player = get_player(self.guild_id)
        if not user_in_bot_channel(interaction.user, player):
            await interaction.response.send_message(
                f"{EMOJI_ERROR} Join the bot's voice channel to use these controls.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(emoji=EMOJI_PAUSE, style=discord.ButtonStyle.secondary, custom_id="furious:pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        if player.voice and player.voice.is_playing():
            player.voice.pause()
            player.status = "Paused"
            await player.update_panel()
            await interaction.response.send_message(f"{EMOJI_PAUSE} Playback paused.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{EMOJI_ERROR} Nothing is currently playing.", ephemeral=True)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.success, custom_id="furious:resume")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        if player.voice and player.voice.is_paused():
            player.voice.resume()
            player.status = "Playing"
            await player.update_panel()
            await interaction.response.send_message("▶️ Playback resumed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{EMOJI_ERROR} Nothing is paused.", ephemeral=True)

    @discord.ui.button(emoji=EMOJI_SKIP, style=discord.ButtonStyle.primary, custom_id="furious:skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        if player.is_active():
            player.skip()
            await interaction.response.send_message(f"{EMOJI_SKIP} Skipped the current track.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{EMOJI_ERROR} Nothing is playing.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="furious:loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        player.loop = not player.loop
        await player.update_panel()
        await interaction.response.send_message(
            f"🔁 Track loop {'enabled' if player.loop else 'disabled'}.",
            ephemeral=True,
        )

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="furious:stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        await player.stop()
        await interaction.response.send_message(f"{EMOJI_TICK} Playback stopped and queue cleared.", ephemeral=True)


# ------------------------------------------------------------
# Stats embed + Trust button
# ------------------------------------------------------------
def build_stats_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎵 Furious Music Bot — Stats",
        color=discord.Color.from_rgb(138, 99, 255),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="👑 Developer", value=f"`{DEV_NAME}`", inline=True)
    embed.add_field(name="⏱️ Uptime", value=f"`{get_uptime()}`", inline=True)
    embed.add_field(name="🏓 Ping", value=f"`{round(bot.latency * 1000)}ms`", inline=True)
    embed.add_field(name="🌐 Servers", value=f"`{len(bot.guilds)}`", inline=True)
    embed.add_field(
        name="👥 Users",
        value=f"`{sum(g.member_count or 0 for g in bot.guilds)}`",
        inline=True,
    )
    embed.add_field(name="🤝 Trusted", value=f"`{len(trusted_users)}`", inline=True)
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text=f"Furious Audio  •  Developed by {DEV_NAME}")
    return embed


class TrustView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(
        label="Trust",
        emoji="🤝",
        style=discord.ButtonStyle.success,
        custom_id="furious:trust",
    )
    async def trust_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in trusted_users:
            return await interaction.response.send_message(
                "⚠️ You already trust me! 💖", ephemeral=True
            )

        trusted_users.add(interaction.user.id)
        save_trusted(trusted_users)

        # update the stats embed with the new trusted count
        await interaction.response.edit_message(embed=build_stats_embed(), view=self)
        await interaction.followup.send(
            "🤝 Trust added! Thanks for trusting **Furious Music Bot** 🎶", ephemeral=True
        )


# ------------------------------------------------------------
# Rotating status (dev / uptime / servers / trusted)
# ------------------------------------------------------------
status_cycle = itertools.cycle(range(4))


@tasks.loop(seconds=15)
async def status_loop():
    statuses = [
        f"👑 Dev: {DEV_NAME}",
        f"⏱️ Uptime: {get_uptime()}",
        f"🌐 {len(bot.guilds)} Servers",
        f"🤝 {len(trusted_users)} Trusted Users",
    ]
    idx = next(status_cycle)
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=statuses[idx],
        ),
    )


@status_loop.before_loop
async def before_status_loop():
    await bot.wait_until_ready()


# ------------------------------------------------------------
# Bot
# ------------------------------------------------------------
class FuriousBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(TrustView())  # keeps the Trust button working after restarts
        try:
            synced = await self.tree.sync()
            log.info("Synced %s slash commands.", len(synced))
        except Exception:
            log.exception("Slash command sync failed.")
        if not status_loop.is_running():
            status_loop.start()


intents = discord.Intents.default()
intents.message_content = True  # REQUIRED for prefix commands (enable in Developer Portal too)
bot = FuriousBot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    help_command=None,
    case_insensitive=True,
)
bot.start_time = time.time()


@bot.event
async def on_ready():
    log.info("FURIOUS ONLINE as %s", bot.user)


# Just pinging the bot -> stats embed + Trust button
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user and message.content.strip() in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        try:
            await message.reply(embed=build_stats_embed(), view=TrustView(), mention_author=False)
        except discord.HTTPException:
            pass
        return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"{EMOJI_ERROR} Missing argument: `{error.param.name}`")
    if isinstance(error, commands.NoPrivateMessage):
        return await ctx.send(f"{EMOJI_ERROR} This command only works in servers.")
    log.error("Command error in %s: %s", ctx.command, error)


@bot.event
async def on_voice_state_update(member, before, after):
    if not (bot.user and member.id == bot.user.id and before.channel and not after.channel):
        return

    player = players.get(member.guild.id)
    if not player:
        return

    player.queue.clear()
    remove_song_file(player.current)
    player.current = None
    player.voice = None
    player.status = "Idle"
    player.skipped = False
    await player.update_panel()

    # 24/7 mode: if we got dropped/kicked, jump straight back in.
    if player.always_on and player.voice_channel_id:
        await asyncio.sleep(2)
        guild = member.guild
        channel = guild.get_channel(player.voice_channel_id)
        if channel and guild.voice_client is None:
            try:
                player.voice = await channel.connect()
                log.info("[%s] 24/7 mode: rejoined %s", guild.id, channel.name)
            except Exception:
                log.exception("24/7 mode: could not rejoin voice channel.")


# ------------------------------------------------------------
# Commands (hybrid = works as /command AND @Furious command)
# ------------------------------------------------------------
@bot.hybrid_command(name="play", aliases=["p"], description="Play a song or add it to the queue")
@commands.guild_only()
@app_commands.describe(query="Song name or supported audio URL")
async def play_cmd(ctx: commands.Context, *, query: str):
    await ctx.defer()

    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send(f"{EMOJI_ERROR} Join a voice channel first.")

    player = get_player(ctx.guild.id)
    channel = ctx.author.voice.channel

    try:
        voice = ctx.guild.voice_client
        if voice is None:
            voice = await channel.connect()
        elif voice.channel != channel:
            await voice.move_to(channel)

        player.voice = voice
        player.text_channel = ctx.channel
        if player.always_on:
            player.voice_channel_id = channel.id

        song = await extract_song(query)
        if not song:
            return await ctx.send(f"{EMOJI_ERROR} I couldn't find a matching track.")

        song["requester"] = ctx.author.display_name
        was_busy = player.is_active() or bool(player.queue) or bool(player.current)
        await player.add(song)

        if was_busy:
            await ctx.send(f"{EMOJI_TICK} Added **{song['title']}** to the queue.")
        else:
            await ctx.send(f"{EMOJI_SPOTIFY} Loading **{song['title']}**...")
    except Exception as error:
        log.exception("play failed")
        await ctx.send(f"{EMOJI_ERROR} Something went wrong: `{str(error)[:250]}`")


@bot.hybrid_command(name="pause", description="Pause the current track")
@commands.guild_only()
async def pause_cmd(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not user_in_bot_channel(ctx.author, player):
        return await ctx.send(f"{EMOJI_ERROR} Join the bot's voice channel first.", ephemeral=True)
    if player.voice and player.voice.is_playing():
        player.voice.pause()
        player.status = "Paused"
        await player.update_panel()
        return await ctx.send(f"{EMOJI_PAUSE} Paused.")
    await ctx.send(f"{EMOJI_ERROR} Nothing is playing.", ephemeral=True)


@bot.hybrid_command(name="resume", aliases=["r"], description="Resume the current track")
@commands.guild_only()
async def resume_cmd(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not user_in_bot_channel(ctx.author, player):
        return await ctx.send(f"{EMOJI_ERROR} Join the bot's voice channel first.", ephemeral=True)
    if player.voice and player.voice.is_paused():
        player.voice.resume()
        player.status = "Playing"
        await player.update_panel()
        return await ctx.send("▶️ Resumed.")
    await ctx.send(f"{EMOJI_ERROR} Nothing is paused.", ephemeral=True)


@bot.hybrid_command(name="skip", aliases=["s"], description="Skip the current track")
@commands.guild_only()
async def skip_cmd(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not user_in_bot_channel(ctx.author, player):
        return await ctx.send(f"{EMOJI_ERROR} Join the bot's voice channel first.", ephemeral=True)
    if player.is_active():
        player.skip()
        return await ctx.send(f"{EMOJI_SKIP} Skipped.")
    await ctx.send(f"{EMOJI_ERROR} Nothing is playing.", ephemeral=True)


@bot.hybrid_command(name="stop", description="Stop playback and clear the queue")
@commands.guild_only()
async def stop_cmd(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not user_in_bot_channel(ctx.author, player):
        return await ctx.send(f"{EMOJI_ERROR} Join the bot's voice channel first.", ephemeral=True)
    await player.stop()
    await ctx.send(f"{EMOJI_TICK} Playback stopped and queue cleared.")


@bot.hybrid_command(name="queue", aliases=["q"], description="Show the current music queue")
@commands.guild_only()
async def queue_cmd(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    embed = discord.Embed(title="FURIOUS  /  QUEUE", color=EMBED_COLOR)

    if player.current:
        embed.add_field(
            name=f"{EMOJI_SPOTIFY} NOW PLAYING",
            value=f"[{discord.utils.escape_markdown(player.current['title'])}]({player.current['webpage']})",
            inline=False,
        )
    if player.queue:
        lines = []
        for index, song in enumerate(player.queue[:10], start=1):
            lines.append(f"`{index:02}`  [{discord.utils.escape_markdown(song['title'])[:90]}]({song['webpage']})")
        embed.add_field(name="UP NEXT", value="\n".join(lines), inline=False)
        if len(player.queue) > 10:
            embed.set_footer(text=f"And {len(player.queue) - 10} more track(s)")
    elif not player.current:
        embed.description = "The queue is empty. Add a track with **/play**."

    await ctx.send(embed=embed)


@bot.hybrid_command(name="loop", description="Toggle repeat for the current track")
@commands.guild_only()
async def loop_cmd(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    player.loop = not player.loop
    await player.update_panel()
    await ctx.send(f"🔁 Loop {'enabled' if player.loop else 'disabled'}.")


@bot.hybrid_command(name="volume", aliases=["vol"], description="Set the volume for the next track")
@commands.guild_only()
@app_commands.describe(level="Volume from 1 to 100")
async def volume_cmd(ctx: commands.Context, level: app_commands.Range[int, 1, 100]):
    player = get_player(ctx.guild.id)
    player.volume = level / 100
    await player.update_panel()
    await ctx.send(f"{EMOJI_TICK} Volume set to **{level}%**. It will apply from the next track.")


@bot.hybrid_command(name="nowplaying", aliases=["np"], description="Show the current player panel")
@commands.guild_only()
async def nowplaying_cmd(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not player.current:
        return await ctx.send(f"{EMOJI_ERROR} Nothing is playing.", ephemeral=True)
    await ctx.send(
        embed=make_player_embed(player, player.status),
        view=PlayerView(ctx.guild.id),
    )


@bot.hybrid_command(name="247", aliases=["stay", "twentyfourseven"], description="Toggle 24/7 mode (bot never leaves voice)")
@commands.guild_only()
async def mode_247_cmd(ctx: commands.Context):
    await ctx.defer()
    player = get_player(ctx.guild.id)

    # ---- turn OFF ----
    if player.always_on:
        player.always_on = False
        player.voice_channel_id = None
        await player.update_panel()
        if not player.is_active() and not player.queue:
            player.schedule_idle_disconnect()
        return await ctx.send(f"{EMOJI_TICK} 24/7 mode **disabled**. I'll leave after being idle.")

    # ---- turn ON ----
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send(f"{EMOJI_ERROR} Join a voice channel first.")

    channel = ctx.author.voice.channel
    try:
        voice = ctx.guild.voice_client
        if voice is None:
            voice = await channel.connect()
        elif voice.channel != channel:
            await voice.move_to(channel)
    except Exception as error:
        log.exception("247 failed")
        return await ctx.send(f"{EMOJI_ERROR} Couldn't join the voice channel: `{str(error)[:250]}`")

    player.voice = voice
    player.text_channel = ctx.channel
    player.always_on = True
    player.voice_channel_id = channel.id
    if player.idle_task and not player.idle_task.done():
        player.idle_task.cancel()

    await player.update_panel()
    await ctx.send(f"{EMOJI_TICK} 24/7 mode **enabled** in **{channel.name}**. I won't leave this channel.")


# ------------------------------------------------------------
# /trust (command version, same as the button)
# ------------------------------------------------------------
@bot.hybrid_command(name="trust", description="Trust Furious Music Bot")
async def trust_cmd(ctx: commands.Context):
    if ctx.author.id in trusted_users:
        embed = discord.Embed(
            title="⚠️ Already Trusted",
            description=f"{ctx.author.mention}, you already trust me! 💖",
            color=discord.Color.from_rgb(255, 184, 0),
        )
    else:
        trusted_users.add(ctx.author.id)
        save_trusted(trusted_users)
        embed = discord.Embed(
            title="🤝 Trust Added!",
            description=f"{ctx.author.mention} now trusts **Furious Music Bot**! 🎶",
            color=discord.Color.from_rgb(0, 230, 118),
        )
    embed.add_field(name="🛡️ Total Trusted", value=f"`{len(trusted_users)}`", inline=True)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.set_footer(text=f"Developed by {DEV_NAME}")
    await ctx.send(embed=embed)


# ------------------------------------------------------------
# /stats -> stats embed + Trust button
# ------------------------------------------------------------
@bot.hybrid_command(name="stats", aliases=["info", "botinfo"], description="Show bot statistics")
async def stats_cmd(ctx: commands.Context):
    await ctx.send(embed=build_stats_embed(), view=TrustView())


# ------------------------------------------------------------
# /ping
# ------------------------------------------------------------
@bot.hybrid_command(name="ping", description="Check the bot latency")
async def ping_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: `{round(bot.latency * 1000)}ms`",
        color=discord.Color.from_rgb(0, 230, 118),
    )
    embed.set_footer(text=f"Developed by {DEV_NAME}")
    await ctx.send(embed=embed)


# ------------------------------------------------------------
# /help
# ------------------------------------------------------------
@bot.hybrid_command(name="help", description="Show Furious music commands")
@commands.guild_only()
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="FURIOUS  /  COMMANDS",
        description=f"Use **/command** or **{PREFIX}command**.",
        color=EMBED_COLOR,
    )
    embed.add_field(
        name=f"{EMOJI_KNIFE}  PLAYBACK",
        value=(
            f"`/play` `{PREFIX}p`  Search or play a track\n"
            f"`/pause`  Pause playback\n"
            f"`/resume` `{PREFIX}r`  Resume playback\n"
            f"`/skip` `{PREFIX}s`  Skip current track\n"
            f"`/stop`  Stop and clear queue"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{EMOJI_SPOTIFY}  PLAYER",
        value=(
            f"`/queue` `{PREFIX}q`  View upcoming tracks\n"
            f"`/nowplaying` `{PREFIX}np`  Open player panel\n"
            f"`/loop`  Toggle track repeat\n"
            f"`/volume` `{PREFIX}vol`  Set next-track volume\n"
            f"`/247` `{PREFIX}247`  Toggle 24/7 mode (never leave VC)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🤖  BOT",
        value=(
            "`/trust`  Trust Furious Music Bot\n"
            "`/stats`  Bot stats + Trust button\n"
            "`/ping`  Check latency"
        ),
        inline=False,
    )
    embed.set_footer(text=f"FURIOUS AUDIO  •  Developed by {DEV_NAME}")
    await ctx.send(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Set it in your local .env file.")
    if not FFMPEG_PATH:
        raise RuntimeError("FFmpeg was not found. Install FFmpeg and add it to PATH.")

    startup_check()
    clean_downloads()
    bot.run(TOKEN)  
