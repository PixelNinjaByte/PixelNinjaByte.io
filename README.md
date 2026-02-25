# Discord Study Bot (Python)

A focused Discord bot for study sessions with:
- Shared study voice channel creation
- Session start/end controls
- Auto server-mute enforcement inside the study channel
- Study time tracking and server leaderboard
- Pomodoro focus/break cycles
- Weekly leaderboard and weekly reset

## Requirements
- Python 3.11+
- A Discord bot token
- Bot permissions in server:
  - Manage Channels
  - Move Members
  - Mute Members
  - View Channels / Connect / Send Messages

## Setup
1. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. Copy env template:
   ```powershell
   Copy-Item .env.example .env
   ```
3. Edit `.env` and set:
   - `DISCORD_BOT_TOKEN`
4. Enable **Server Members Intent** in the Discord Developer Portal for your bot.
5. Run:
   ```powershell
   python src/bot.py
   ```

## Slash Commands
- `/setup_study` (admin): Create/restore the shared study room.
- `/start_study` (mods): Start session and enforce mute in study room.
- `/join_study`: Move yourself into the shared study room.
- `/end_study` (mods): End session and store duration.
- `/leaderboard`: Show top study users in server.
- `/weekly_leaderboard`: Show top study users for the current UTC week (starts Monday).
- `/weekly_reset` (admin): Reset this week's leaderboard data.
- `/my_study_time`: Show your accumulated study time.
- `/pomodoro_start`: Start Pomodoro cycles in the study room.
- `/pomodoro_stop`: Stop active Pomodoro and end study session.

## Suggested Improvements
- Daily streaks and XP badges.
- Optional text-channel lockdown during active study sessions.
- Per-user opt-in reminders and scheduled sessions.
- Weekly report embeds to a chosen channel.
