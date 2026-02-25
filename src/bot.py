import asyncio
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer



DB_PATH = "studybot.db"
DEFAULT_CATEGORY_NAME = "Study Sessions"
DEFAULT_VOICE_NAME = "Focused Study Room"


def week_start_utc(dt: datetime) -> date:
    dt_utc = dt.astimezone(timezone.utc)
    return (dt_utc - timedelta(days=dt_utc.weekday())).date()


@dataclass
class ActiveSession:
    guild_id: int
    voice_channel_id: int
    started_at: datetime
    enforce_mute_task: asyncio.Task | None = None


@dataclass
class PomodoroState:
    task: asyncio.Task
    text_channel_id: int
    work_minutes: int
    break_minutes: int
    cycles: int

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


class StudyStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def setup(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS study_time (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    total_seconds INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS weekly_study_time (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    week_start TEXT NOT NULL,
                    total_seconds INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id, week_start)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS session_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    voice_channel_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    duration_seconds INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id INTEGER PRIMARY KEY,
                    study_voice_channel_id INTEGER,
                    study_category_id INTEGER
                )
                """
            )
            await db.commit()

    async def upsert_guild_config(
        self,
        guild_id: int,
        study_voice_channel_id: int,
        study_category_id: int,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO guild_config (guild_id, study_voice_channel_id, study_category_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    study_voice_channel_id = excluded.study_voice_channel_id,
                    study_category_id = excluded.study_category_id
                """,
                (guild_id, study_voice_channel_id, study_category_id),
            )
            await db.commit()

    async def get_guild_config(self, guild_id: int) -> tuple[int | None, int | None]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT study_voice_channel_id, study_category_id FROM guild_config WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None, None
            return row[0], row[1]

    async def create_session_record(self, guild_id: int, voice_channel_id: int, started_at: datetime) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO session_history (guild_id, voice_channel_id, started_at)
                VALUES (?, ?, ?)
                """,
                (guild_id, voice_channel_id, started_at.isoformat()),
            )
            await db.commit()
            return cursor.lastrowid

    async def close_session_record(self, session_id: int, ended_at: datetime, duration_seconds: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE session_history
                SET ended_at = ?, duration_seconds = ?
                WHERE id = ?
                """,
                (ended_at.isoformat(), duration_seconds, session_id),
            )
            await db.commit()

    async def add_study_seconds(self, guild_id: int, user_id: int, seconds: int, at_time: datetime) -> None:
        if seconds <= 0:
            return
        start_of_week = week_start_utc(at_time).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO study_time (guild_id, user_id, total_seconds)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET total_seconds = total_seconds + excluded.total_seconds
                """,
                (guild_id, user_id, seconds),
            )
            await db.execute(
                """
                INSERT INTO weekly_study_time (guild_id, user_id, week_start, total_seconds)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, week_start)
                DO UPDATE SET total_seconds = total_seconds + excluded.total_seconds
                """,
                (guild_id, user_id, start_of_week, seconds),
            )
            await db.commit()

    async def get_top_users(self, guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT user_id, total_seconds
                FROM study_time
                WHERE guild_id = ?
                ORDER BY total_seconds DESC
                LIMIT ?
                """,
                (guild_id, limit),
            )
            rows = await cursor.fetchall()
            return [(row[0], row[1]) for row in rows]

    async def get_weekly_top_users(self, guild_id: int, week_start: date, limit: int = 10) -> list[tuple[int, int]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT user_id, total_seconds
                FROM weekly_study_time
                WHERE guild_id = ? AND week_start = ?
                ORDER BY total_seconds DESC
                LIMIT ?
                """,
                (guild_id, week_start.isoformat(), limit),
            )
            rows = await cursor.fetchall()
            return [(row[0], row[1]) for row in rows]

    async def reset_weekly_data(self, guild_id: int, week_start: date) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM weekly_study_time WHERE guild_id = ? AND week_start = ?",
                (guild_id, week_start.isoformat()),
            )
            await db.commit()
            return cursor.rowcount

    async def get_user_seconds(self, guild_id: int, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT total_seconds FROM study_time WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


class StudyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

        self.store = StudyStore(DB_PATH)
        self.active_sessions: dict[int, ActiveSession] = {}
        self.active_session_record_id: dict[int, int] = {}
        self.enforced_user_ids: dict[int, set[int]] = {}
        self.session_joined_at: dict[int, dict[int, datetime]] = {}
        self.focus_mode_active: dict[int, bool] = {}
        self.pomodoro_states: dict[int, PomodoroState] = {}

    async def setup_hook(self) -> None:
        await self.store.setup()
        await self.tree.sync()

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (ID: {self.user.id})")


bot = StudyBot()


def fmt_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes}m {secs}s"


async def ensure_study_channel(guild: discord.Guild) -> discord.VoiceChannel:
    channel_id, category_id = await bot.store.get_guild_config(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None

    if isinstance(channel, discord.VoiceChannel):
        return channel

    category = guild.get_channel(category_id) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = discord.utils.get(guild.categories, name=DEFAULT_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(DEFAULT_CATEGORY_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(connect=True, speak=False),
    }
    channel = await guild.create_voice_channel(
        DEFAULT_VOICE_NAME,
        category=category,
        overwrites=overwrites,
        reason="Create shared study voice room",
    )

    await bot.store.upsert_guild_config(guild.id, channel.id, category.id)
    return channel


async def accrue_user_time(guild_id: int, user_id: int, now: datetime) -> None:
    joined_at = bot.session_joined_at.get(guild_id, {}).pop(user_id, None)
    if joined_at is None:
        return
    elapsed = int((now - joined_at).total_seconds())
    await bot.store.add_study_seconds(guild_id, user_id, elapsed, now)


async def enforce_channel_mute(session: ActiveSession) -> None:
    guild = bot.get_guild(session.guild_id)
    if guild is None:
        return

    channel = guild.get_channel(session.voice_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        return

    bot.enforced_user_ids.setdefault(guild.id, set())

    while guild.id in bot.active_sessions:
        try:
            if not bot.focus_mode_active.get(guild.id, True):
                await asyncio.sleep(5)
                continue

            for member in channel.members:
                if member.bot or member.voice is None:
                    continue
                if not member.voice.mute:
                    await member.edit(mute=True, reason="Active study session enforces mute")
                    bot.enforced_user_ids[guild.id].add(member.id)
            await asyncio.sleep(10)
        except discord.Forbidden:
            await asyncio.sleep(10)
        except Exception:
            await asyncio.sleep(10)


async def set_focus_mode(guild: discord.Guild, enabled: bool) -> None:
    session = bot.active_sessions.get(guild.id)
    if session is None:
        return

    channel = guild.get_channel(session.voice_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        return

    now = datetime.now(timezone.utc)
    bot.focus_mode_active[guild.id] = enabled

    if enabled:
        bot.session_joined_at[guild.id] = {
            member.id: now for member in channel.members if not member.bot
        }
        for member in channel.members:
            if member.bot or member.voice is None or member.voice.mute:
                continue
            try:
                await member.edit(mute=True, reason="Pomodoro focus mode started")
                bot.enforced_user_ids.setdefault(guild.id, set()).add(member.id)
            except discord.Forbidden:
                pass
        return

    for user_id in list(bot.session_joined_at.get(guild.id, {}).keys()):
        await accrue_user_time(guild.id, user_id, now)

    for member in channel.members:
        if member.bot:
            continue
        if member.id in bot.enforced_user_ids.get(guild.id, set()) and member.voice and member.voice.mute:
            try:
                await member.edit(mute=False, reason="Pomodoro break started")
            except discord.Forbidden:
                pass


async def start_session(guild: discord.Guild) -> tuple[bool, discord.VoiceChannel | None]:
    if guild.id in bot.active_sessions:
        channel = guild.get_channel(bot.active_sessions[guild.id].voice_channel_id)
        return False, channel if isinstance(channel, discord.VoiceChannel) else None

    channel = await ensure_study_channel(guild)
    started_at = datetime.now(timezone.utc)
    session = ActiveSession(guild.id, channel.id, started_at)
    bot.active_sessions[guild.id] = session
    bot.focus_mode_active[guild.id] = True
    bot.session_joined_at[guild.id] = {
        member.id: started_at for member in channel.members if not member.bot
    }

    session_id = await bot.store.create_session_record(guild.id, channel.id, started_at)
    bot.active_session_record_id[guild.id] = session_id
    session.enforce_mute_task = asyncio.create_task(enforce_channel_mute(session))
    return True, channel


async def stop_session(guild: discord.Guild, reason: str) -> int | None:
    session = bot.active_sessions.pop(guild.id, None)
    if session is None:
        return None

    ended_at = datetime.now(timezone.utc)
    duration_seconds = int((ended_at - session.started_at).total_seconds())

    for user_id in list(bot.session_joined_at.get(guild.id, {}).keys()):
        await accrue_user_time(guild.id, user_id, ended_at)

    channel = guild.get_channel(session.voice_channel_id)
    if isinstance(channel, discord.VoiceChannel):
        for member in channel.members:
            if member.bot:
                continue
            if member.id in bot.enforced_user_ids.get(guild.id, set()) and member.voice and member.voice.mute:
                try:
                    await member.edit(mute=False, reason=reason)
                except discord.Forbidden:
                    pass

    bot.enforced_user_ids.pop(guild.id, None)
    bot.session_joined_at.pop(guild.id, None)
    bot.focus_mode_active.pop(guild.id, None)

    task = session.enforce_mute_task
    if task is not None:
        task.cancel()

    session_id = bot.active_session_record_id.pop(guild.id, None)
    if session_id is not None:
        await bot.store.close_session_record(session_id, ended_at, duration_seconds)

    return duration_seconds


async def run_pomodoro_cycles(
    guild: discord.Guild,
    text_channel_id: int,
    work_minutes: int,
    break_minutes: int,
    cycles: int,
) -> None:
    text_channel = guild.get_channel(text_channel_id)
    if not isinstance(text_channel, discord.abc.Messageable):
        return

    try:
        for cycle in range(1, cycles + 1):
            await set_focus_mode(guild, True)
            await text_channel.send(
                f"Pomodoro cycle {cycle}/{cycles}: focus for {work_minutes} minutes."
            )
            await asyncio.sleep(work_minutes * 60)

            if cycle == cycles:
                break

            await set_focus_mode(guild, False)
            await text_channel.send(
                f"Break time: {break_minutes} minutes. Focus resumes after the break."
            )
            await asyncio.sleep(break_minutes * 60)

        duration = await stop_session(guild, reason="Pomodoro completed")
        if duration is not None:
            await text_channel.send(
                f"Pomodoro finished. Study session ended after {fmt_duration(duration)}."
            )
    except asyncio.CancelledError:
        duration = await stop_session(guild, reason="Pomodoro stopped")
        if duration is not None:
            await text_channel.send(
                f"Pomodoro stopped. Study session ended after {fmt_duration(duration)}."
            )
        raise
    finally:
        bot.pomodoro_states.pop(guild.id, None)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    guild = member.guild
    session = bot.active_sessions.get(guild.id)
    if session is None or member.bot:
        return

    study_channel_id = session.voice_channel_id
    now = datetime.now(timezone.utc)

    left_study = before.channel is not None and before.channel.id == study_channel_id
    joined_study = after.channel is not None and after.channel.id == study_channel_id

    if left_study and not joined_study:
        await accrue_user_time(guild.id, member.id, now)
        if member.id in bot.enforced_user_ids.get(guild.id, set()) and before.mute:
            try:
                await member.edit(mute=False, reason="Left study session channel")
            except discord.Forbidden:
                pass

    if joined_study and not left_study:
        if bot.focus_mode_active.get(guild.id, True):
            bot.session_joined_at.setdefault(guild.id, {})[member.id] = now
            if not after.mute:
                try:
                    await member.edit(mute=True, reason="Joined active study session")
                    bot.enforced_user_ids.setdefault(guild.id, set()).add(member.id)
                except discord.Forbidden:
                    pass


@bot.tree.command(name="setup_study", description="Create or restore the shared study voice channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_study(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    channel = await ensure_study_channel(interaction.guild)
    await interaction.response.send_message(
        f"Study room ready: {channel.mention}",
        ephemeral=True,
    )


@bot.tree.command(name="start_study", description="Start a focused study session")
@app_commands.checks.has_permissions(mute_members=True)
async def start_study(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    started, channel = await start_session(guild)
    if not started:
        await interaction.response.send_message("A study session is already active.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Study session started in {channel.mention}. Everyone in that channel will be server-muted.",
    )


@bot.tree.command(name="join_study", description="Get moved into the shared study voice room")
async def join_study(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    if member.voice is None:
        await interaction.response.send_message("Join any voice channel first, then run this.", ephemeral=True)
        return

    channel = await ensure_study_channel(guild)
    await member.move_to(channel, reason="Joined study session channel")

    await interaction.response.send_message(
        f"Moved you to {channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="end_study", description="End the active study session")
@app_commands.checks.has_permissions(mute_members=True)
async def end_study(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    pomodoro = bot.pomodoro_states.get(guild.id)
    if pomodoro:
        pomodoro.task.cancel()

    duration = await stop_session(guild, reason="Study session ended")
    if duration is None:
        await interaction.response.send_message("No active study session.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Study session ended. Session duration: {fmt_duration(duration)}",
    )


@bot.tree.command(name="pomodoro_start", description="Run Pomodoro focus/break cycles in the study room")
@app_commands.checks.has_permissions(mute_members=True)
@app_commands.describe(work_minutes="Focus minutes per cycle", break_minutes="Break minutes between cycles", cycles="Number of focus cycles")
async def pomodoro_start(
    interaction: discord.Interaction,
    work_minutes: app_commands.Range[int, 5, 120],
    break_minutes: app_commands.Range[int, 1, 60],
    cycles: app_commands.Range[int, 1, 12] = 4,
) -> None:
    guild = interaction.guild
    channel = interaction.channel
    if guild is None or channel is None:
        await interaction.response.send_message("Use this command in a server channel.", ephemeral=True)
        return

    if guild.id in bot.pomodoro_states:
        await interaction.response.send_message("A Pomodoro session is already running.", ephemeral=True)
        return

    started, study_channel = await start_session(guild)
    if not started and guild.id not in bot.active_sessions:
        await interaction.response.send_message("Could not start or find an active study session.", ephemeral=True)
        return

    task = asyncio.create_task(
        run_pomodoro_cycles(
            guild,
            channel.id,
            int(work_minutes),
            int(break_minutes),
            int(cycles),
        )
    )
    bot.pomodoro_states[guild.id] = PomodoroState(
        task=task,
        text_channel_id=channel.id,
        work_minutes=int(work_minutes),
        break_minutes=int(break_minutes),
        cycles=int(cycles),
    )

    await interaction.response.send_message(
        f"Pomodoro started: {work_minutes}m focus / {break_minutes}m break for {cycles} cycles in {study_channel.mention}.",
    )


@bot.tree.command(name="pomodoro_stop", description="Stop the current Pomodoro and end study session")
@app_commands.checks.has_permissions(mute_members=True)
async def pomodoro_stop(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    state = bot.pomodoro_states.get(guild.id)
    if state is None:
        await interaction.response.send_message("No Pomodoro is running.", ephemeral=True)
        return

    state.task.cancel()
    await interaction.response.send_message("Stopping Pomodoro...")


@bot.tree.command(name="leaderboard", description="Top total study times in this server")
async def leaderboard(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    top = await bot.store.get_top_users(guild.id, limit=10)
    if not top:
        await interaction.response.send_message("No study data yet.")
        return

    lines = []
    for i, (user_id, total_seconds) in enumerate(top, start=1):
        user = guild.get_member(user_id)
        name = user.display_name if user else f"User {user_id}"
        lines.append(f"{i}. {name} - {fmt_duration(total_seconds)}")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="weekly_leaderboard", description="Top study times for the current week")
async def weekly_leaderboard(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    this_week = week_start_utc(datetime.now(timezone.utc))
    top = await bot.store.get_weekly_top_users(guild.id, this_week, limit=10)
    if not top:
        await interaction.response.send_message(f"No weekly study data yet for week starting {this_week.isoformat()}.")
        return

    lines = [f"Weekly leaderboard (week starting {this_week.isoformat()}):"]
    for i, (user_id, total_seconds) in enumerate(top, start=1):
        user = guild.get_member(user_id)
        name = user.display_name if user else f"User {user_id}"
        lines.append(f"{i}. {name} - {fmt_duration(total_seconds)}")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="weekly_reset", description="Reset this server's weekly leaderboard for the current week")
@app_commands.checks.has_permissions(manage_guild=True)
async def weekly_reset(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    this_week = week_start_utc(datetime.now(timezone.utc))
    deleted = await bot.store.reset_weekly_data(guild.id, this_week)
    await interaction.response.send_message(
        f"Weekly data reset for week starting {this_week.isoformat()}. Cleared {deleted} entries.",
        ephemeral=True,
    )


@bot.tree.command(name="my_study_time", description="Show your total study time")
async def my_study_time(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    user = interaction.user
    if guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    total_seconds = await bot.store.get_user_seconds(guild.id, user.id)
    await interaction.response.send_message(f"Your total study time: {fmt_duration(total_seconds)}")


@start_study.error
@end_study.error
@setup_study.error
@pomodoro_start.error
@pomodoro_stop.error
@weekly_reset.error
async def permission_error_handler(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You do not have permission for this command.", ephemeral=True)
    else:
        if interaction.response.is_done():
            await interaction.followup.send("Command failed. Check bot permissions and logs.", ephemeral=True)
        else:
            await interaction.response.send_message("Command failed. Check bot permissions and logs.", ephemeral=True)


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN")

    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing. Set it in your environment or .env file.")

    threading.Thread(target=run_health_server, daemon=True).start()
    bot.run(token)



if __name__ == "__main__":
    main()
