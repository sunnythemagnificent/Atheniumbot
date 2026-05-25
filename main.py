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

# Giveaway channel settings
GIVEAWAY_CHANNEL = "🎁︱giveaways"          # Channel name without #
GIVEAWAY_BOT_ROLE = "GiveawayBot"          # Role name of the giveaway bot — must match exactly
GIVEAWAY_DELETE_SECONDS = 600              # Delete messages after 10 minutes
GIVEAWAY_PING_ROLE = "giveaways"           # Role to ping when a new giveaway is detected

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

# Track which giveaway messages we've already pinged for (avoid double pinging)
pinged_giveaways = set()


# ============================================================
#  HELPER — check and ping for fresh giveaway
# ============================================================

async def maybe_ping_giveaway(message):
    if message.channel.name != GIVEAWAY_CHANNEL:
        return
    if not message.author.bot:
        return
    if message.id in pinged_giveaways:
        print(f"⏭️ Already pinged for message {message.id}, skipping")
        return

    author_role_names = [r.name for r in getattr(message.author, 'roles', [])]
    if GIVEAWAY_BOT_ROLE not in author_role_names:
        return

    # Check embeds for "Ends:" but not "Ended:"
    embed_text = ""
    for embed in message.embeds:
        if embed.title:
            embed_text += embed.title + " "
        if embed.description:
            embed_text += embed.description + " "
        if embed.footer and embed.footer.text:
            embed_text += embed.footer.text + " "
        for field in embed.fields:
            embed_text += field.name + " " + field.value + " "

    print(f"🔍 GiveawayBot embed text: '{embed_text[:200]}'")

    if "Ends:" in embed_text and "Ended:" not in embed_text:
        ping_role = discord.utils.get(message.guild.roles, name=GIVEAWAY_PING_ROLE)
        if ping_role:
            pinged_giveaways.add(message.id)
            await message.channel.send(ping_role.mention)
            print(f"🎉 Pinged @{GIVEAWAY_PING_ROLE} for new giveaway in #{message.channel.name}")


# ============================================================
#  EVENTS
# ============================================================

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    print(f"📋 Watching for activity | Role: '{ACTIVE_ROLE_NAME}' | Window: {ACTIVE_DURATION_DAYS} days")
    client.loop.create_task(check_expirations())


@client.event
async def on_message(message):
    if message.guild is None:
        return

    # --------------------------------------------------------
    #  GIVEAWAY CHANNEL
    # --------------------------------------------------------
    if message.channel.name == GIVEAWAY_CHANNEL:

        # Never touch the bot's own messages
        if message.author == client.user:
            return

        if message.author.bot:
            # Try to ping immediately (works if embed is already attached)
            await maybe_ping_giveaway(message)
            return

        # Regular member message — silently delete after 10 minutes
        async def delayed_delete(msg):
            await asyncio.sleep(GIVEAWAY_DELETE_SECONDS)
            try:
                await msg.delete()
                print(f"🗑️ Silently deleted message from {msg.author.display_name} in #{msg.channel.name}")
            except Exception as e:
                print(f"⚠️ Could not delete message: {e}")

        asyncio.ensure_future(delayed_delete(message))
        return

    # Ignore all other bot messages
    if message.author.bot:
        return

    # Ignore configured channels for activity tracking
    if message.channel.name in IGNORED_CHANNELS:
        return

    # --------------------------------------------------------
    #  ACTIVE ROLE TRACKING
    # --------------------------------------------------------
    member = message.author
    role = discord.utils.get(message.guild.roles, name=ACTIVE_ROLE_NAME)

    if role is None:
        print(f"⚠️  Role '{ACTIVE_ROLE_NAME}' not found. Check the name matches exactly.")
        return

    expiry = datetime.now(timezone.utc) + timedelta(days=ACTIVE_DURATION_DAYS)
    expiry_times[member.id] = expiry

    if role not in member.roles:
        await member.add_roles(role)
        print(f"✅ Gave '{ACTIVE_ROLE_NAME}' to {member.display_name}")


@client.event
async def on_message_edit(before, after):
    # GiveawayBot often edits its message to attach the embed
    # This catches it the moment the embed appears
    if after.guild is None:
        return
    if after.channel.name != GIVEAWAY_CHANNEL:
        return
    if not after.author.bot:
        return
    await maybe_ping_giveaway(after)


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
#  RUN
# ============================================================

client.run(BOT_TOKEN)
