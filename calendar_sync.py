"""
calendar_sync.py - Sends the guild's birthdays to the mod calendar on the website
=================================================================================

Every few minutes the bot reads its own birthdays table (set by /setbirthday),
looks up each member's current display name in the main server, and sends the
website a complete snapshot of "who has a birthday on which month and day".
The calendar page uses that to show birthdays (mods only).

PRIVACY
- Only the month and day are read from the database. The birth-year column is
  deliberately never selected, so a year can't be sent even by accident.
- Members who have left the server are not sent.
- Because every sync is a COMPLETE snapshot, anyone who uses /removebirthday
  disappears from the calendar on the next sync (within about five minutes).

No new setting is needed on Railway: it reuses ACTIVITY_SYNC_SECRET, the same
shared secret your other website syncs already use.

HOOKS NEEDED IN main.py (2 small edits):
    import calendar_sync                                              (near the top)
    calendar_sync.start_background(bot, get_db, MAIN_GUILD_NAME)      (in on_ready, with the other tasks)

(This file is named calendar_sync.py on purpose. Python has a built-in module
called "calendar" that main.py already uses, so never name a file calendar.py.)
"""

import asyncio
import hashlib
import json
import os
import time

import aiohttp
import discord

# ============================================================
#  CONFIGURATION
# ============================================================

BIRTHDAY_SYNC_URL = os.environ.get("BIRTHDAY_SYNC_URL", "https://mods.athenaeumarchive.com/birthdays_sync.php")
SYNC_SECRET_ENV = "ACTIVITY_SYNC_SECRET"   # the same shared secret your other website syncs use

CHECK_INTERVAL_SECONDS = 300      # look for changes every 5 minutes
RESEND_AFTER_SECONDS = 3600       # also re-send an unchanged list hourly, in case the website missed one
MAX_NAME_LENGTH = 100


# ============================================================
#  BUILDING THE SNAPSHOT
# ============================================================

def build_snapshot(rows, guild):
    """Turns database rows into the payload the website expects.
    Only members currently in the server are included."""
    entries = []
    for row in rows:
        member = guild.get_member(row["user_id"])
        if member is None or getattr(member, "bot", False):
            continue
        # Collapse any newlines/odd spacing and cap the length
        name = " ".join((member.display_name or "").split())[:MAX_NAME_LENGTH]
        if not name:
            continue
        entries.append({
            "discord_id": str(member.id),
            "display_name": name,
            "month": int(row["birth_month"]),
            "day": int(row["birth_day"]),
        })
    entries.sort(key=lambda e: int(e["discord_id"]))   # stable order so unchanged lists look unchanged
    return {"full_snapshot": True, "birthdays": entries}


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


# ============================================================
#  THE SYNCER
# ============================================================

class BirthdaySyncer:
    def __init__(self, bot, get_db, guild_name, url=None, clock=time.monotonic):
        self.bot = bot
        self.get_db = get_db
        self.guild_name = guild_name
        self.url = url or BIRTHDAY_SYNC_URL
        self.clock = clock
        self.last_digest = None      # what the website last successfully received
        self.last_ok_at = None
        self._warned_no_secret = False
        self._warned_no_guild = False

    def _read_rows(self):
        # Month and day ONLY. The birth_year column is never selected.
        conn = self.get_db()
        try:
            return conn.execute("SELECT user_id, birth_month, birth_day FROM birthdays").fetchall()
        finally:
            conn.close()

    async def sync_once(self, force=False):
        """One check. Returns 'sent', 'unchanged', 'failed', 'no secret', or 'no guild'."""
        secret = os.environ.get(SYNC_SECRET_ENV)
        if not secret:
            if not self._warned_no_secret:
                print(f"⚠️ {SYNC_SECRET_ENV} not set - skipping birthday sync")
                self._warned_no_secret = True
            return "no secret"

        guild = discord.utils.get(self.bot.guilds, name=self.guild_name)
        if guild is None:
            if not self._warned_no_guild:
                print(f"⚠️ Birthday sync: couldn't find the server named '{self.guild_name}'")
                self._warned_no_guild = True
            return "no guild"
        if not guild.chunked:
            await guild.chunk()

        payload = build_snapshot(self._read_rows(), guild)
        digest = _digest(payload)

        now = self.clock()
        due = (
            force
            or digest != self.last_digest
            or self.last_ok_at is None
            or (now - self.last_ok_at) >= RESEND_AFTER_SECONDS
        )
        if not due:
            return "unchanged"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.url,
                    json=payload,
                    headers={"X-Sync-Secret": secret},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    try:
                        result = await resp.json(content_type=None)
                    except Exception:
                        result = {}
                    if resp.status == 200 and isinstance(result, dict) and result.get("success"):
                        self.last_digest = digest
                        self.last_ok_at = now
                        print(f"🎂 Birthday sync: sent {len(payload['birthdays'])} birthday(s) "
                              f"(stored {result.get('stored')}, removed {result.get('removed')})")
                        return "sent"
                    problem = result.get("error") if isinstance(result, dict) else None
                    print(f"⚠️ Birthday sync rejected by the website (HTTP {resp.status}): {problem or 'no details'}")
                    return "failed"
        except Exception as e:
            print(f"⚠️ Birthday sync couldn't reach the website: {e}")
            return "failed"

    async def loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.sync_once()
            except Exception as e:
                print(f"⚠️ Birthday sync error: {e}")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_background(bot, get_db, main_guild_name):
    """Call once from on_ready. Starts the periodic birthday sync."""
    syncer = BirthdaySyncer(bot, get_db, main_guild_name)
    bot.loop.create_task(syncer.loop())
    print("🎂 Birthday sync for the mod calendar started")
    return syncer
