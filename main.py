import discord
import asyncio
import os
from datetime import datetime, timedelta, timezone

# ============================================================
#  CONFIGURATION — edit these values
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

ACTIVE_ROLE_NAME = "Active"          # Must match your role name exactly
ACTIVE_DURATION_DAYS = 30            # Days before role is removed

# Channel names to ignore (no # symbol needed)
IGNORED_CHANNELS = [
    "roles",
    "directory",
    "bookmarks",
    "announcemnts",        # keeping your spelling so it matches your server
    "🛡️︱council",
    "💍︱mudae",
]

# Channels to post the reminder in (no # symbol needed)
REMINDER_CHANNELS = [
    "🎨︱art",
    "🎨︱art2",
    "🏠︱character-clubhouse",
    "📝︱writing",
]

REMINDER_INTERVAL_HOURS = 24
REMINDER_RETRY_HOURS = 2

# Giveaway channel settings
GIVEAWAY_CHANNEL = "🎁︱giveaways"          # Channel name without #
GIVEAWAY_BOT_ROLE = "GiveawayBot"          # Role name of the giveaway bot — must match exactly
GIVEAWAY_DELETE_SECONDS = 600              # Delete messages after 10 minutes

REMINDER_MESSAGE = """Friendly reminder from us here at Athenaeum to respect others posting of their work! Try to make sure you are:

• Compliment/comment on those who have posted above you.
• Try not to steal the spotlight from others who may be scared to share.
• Make sure you're giving more than you take from the guild and group.

We want everyone to be comfortable and feel encouraged here! Thanks for helping us make this community great!"""

# ============================================================
#  BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)

# Stores when each user's Active role should expire
# Format: { user_id: datetime }
expiry_times = {}


# ============================================================
#  EVENTS
# ============================================================

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    print(f"📋 Watching for activity | Role: '{ACTIVE_ROLE_NAME}' | Window: {ACTIVE_DURATION_DAYS} days")
    client.loop.create_task(check_expirations())
    client.loop.create_task(send_reminders())


@client.event
async def on_message(message):
    # Ignore messages from bots UNLESS it's in the giveaway channel
    # (we still need to process giveaway channel messages from real users)
    if message.author.bot:
        return

    guild = message.guild
    if guild is None:
        return

    # --------------------------------------------------------
    #  GIVEAWAY CHANNEL — silently delete non-giveaway messages
    # --------------------------------------------------------
    if message.channel.name == GIVEAWAY_CHANNEL:
        # Check if the author has the GiveawayBot role
        author_roles = [r.name for r in message.author.roles]
        if GIVEAWAY_BOT_ROLE in author_roles:
            # It's the giveaway bot — leave it alone
            return

        # Regular member message — wait 10 minutes then silently delete
        async def delayed_delete(msg):
            await asyncio.sleep(GIVEAWAY_DELETE_SECONDS)
            try:
                await msg.delete()
                print(f"🗑️ Silently deleted message from {msg.author.display_name} in #{msg.channel.name}")
            except Exception as e:
                print(f"⚠️ Could not delete message: {e}")

        asyncio.ensure_future(delayed_delete(message))
        return

    # Ignore configured channels for activity tracking
    if message.channel.name in IGNORED_CHANNELS:
        return

    member = message.author
    role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)

    if role is None:
        print(f"⚠️  Role '{ACTIVE_ROLE_NAME}' not found. Check the name matches exactly.")
        return

    # Set or refresh the expiry time
    expiry = datetime.now(timezone.utc) + timedelta(days=ACTIVE_DURATION_DAYS)
    expiry_times[member.id] = expiry

    # Assign the role if they don't already have it
    if role not in member.roles:
        await member.add_roles(role)
        print(f"✅ Gave '{ACTIVE_ROLE_NAME}' to {member.display_name}")


# ============================================================
#  BACKGROUND TASK — checks for expired roles every hour
# ============================================================

async def check_expirations():
    await client.wait_until_ready()
    while not client.is_closed():
        now = datetime.now(timezone.utc)
        to_remove = [uid for uid, exp in expiry_times.items() if now >= exp]

        for user_id in to_remove:
            del expiry_times[user_id]
            for guild in client.guilds:
                member = guild.get_member(user_id)
                if member:
                    role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)
                    if role and role in member.roles:
                        await member.remove_roles(role)
                        print(f"⏰ Removed '{ACTIVE_ROLE_NAME}' from {member.display_name} (inactive 30 days)")

        await asyncio.sleep(3600)  # Check every hour


# ============================================================
#  BACKGROUND TASK — posts reminder message every 24 hours
# ============================================================

async def send_reminders():
    await client.wait_until_ready()

    while not client.is_closed():
        now = datetime.now(timezone.utc)
        next_sleep = REMINDER_INTERVAL_HOURS * 3600  # default 24h

        for guild in client.guilds:
            for channel in guild.text_channels:
                if channel.name in REMINDER_CHANNELS:
                    try:
                        # Find the bot's last message in this channel
                        bot_last_post = None
                        async for msg in channel.history(limit=50):
                            if msg.author == client.user:
                                bot_last_post = msg.created_at
                                break

                        if bot_last_post:
                            time_since_post = (now - bot_last_post).total_seconds()
                            time_until_next = (REMINDER_INTERVAL_HOURS * 3600) - time_since_post

                            if time_until_next > 0:
                                # Not yet time — check if someone posted after the bot
                                last_message = [msg async for msg in channel.history(limit=1)]
                                if last_message and last_message[0].author == client.user:
                                    # Bot still last — retry in 2h
                                    hours_left = round(time_until_next / 3600, 1)
                                    print(f"⏭️ #{channel.name} — bot was last, {hours_left}h left, retry in {REMINDER_RETRY_HOURS}h")
                                    next_sleep = min(next_sleep, REMINDER_RETRY_HOURS * 3600)
                                else:
                                    # Someone posted after bot — wait remaining time
                                    hours_left = round(time_until_next / 3600, 1)
                                    print(f"⏳ #{channel.name} — new posts, {hours_left}h left on schedule")
                                    next_sleep = min(next_sleep, time_until_next)
                                continue

                        # Either never posted or 24h has passed — send reminder
                        await channel.send(REMINDER_MESSAGE)
                        print(f"📢 Sent reminder to #{channel.name}")

                    except Exception as e:
                        print(f"⚠️ Could not send to #{channel.name}: {e}")

        print(f"⏰ Next check in {round(next_sleep / 3600, 1)} hours")
        await asyncio.sleep(next_sleep)


# ============================================================
#  RUN
# ============================================================

client.run(BOT_TOKEN)
