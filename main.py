import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import sqlite3
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
GIVEAWAY_PING_ROLE = "Giveaways"           # Role to ping when a new giveaway is detected

# Where the persistent database lives — this should point inside your Railway Volume
DB_PATH = os.environ.get("DB_PATH", "/data/atheniumbot.db")

# ============================================================
#  DATABASE SETUP
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # Make sure the folder exists (in case the volume isn't mounted yet)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS art_trade_pool (
            user_id INTEGER PRIMARY KEY,
            size TEXT NOT NULL,
            style TEXT NOT NULL,
            medium TEXT NOT NULL,
            character TEXT NOT NULL,
            match_size INTEGER NOT NULL,
            match_style INTEGER NOT NULL,
            match_medium INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commissions (
            user_id INTEGER PRIMARY KEY,
            slots INTEGER,
            where_link TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print(f"💾 Database ready at {DB_PATH}")


def db_add_entry(entry):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO art_trade_pool
        (user_id, size, style, medium, character, match_size, match_style, match_medium)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        entry["user_id"],
        ",".join(entry["size"]), ",".join(entry["style"]), ",".join(entry["medium"]),
        entry["character"],
        0, 0, 0,  # legacy columns, unused now that matching is list-based
    ))
    conn.commit()
    conn.close()


def db_remove_entry(user_id):
    conn = get_db()
    conn.execute("DELETE FROM art_trade_pool WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def db_load_all_entries():
    conn = get_db()
    rows = conn.execute("SELECT * FROM art_trade_pool").fetchall()
    conn.close()
    entries = {}
    for row in rows:
        entries[row["user_id"]] = {
            "user_id": row["user_id"],
            "size": row["size"].split(",") if row["size"] else [],
            "style": row["style"].split(",") if row["style"] else [],
            "medium": row["medium"].split(",") if row["medium"] else [],
            "character": row["character"],
        }
    return entries


# --- Commission helpers ---

def db_set_commission(user_id, slots, where_link):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO commissions (user_id, slots, where_link)
        VALUES (?, ?, ?)
    """, (user_id, slots, where_link))
    conn.commit()
    conn.close()


def db_remove_commission(user_id):
    conn = get_db()
    conn.execute("DELETE FROM commissions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def db_get_commission(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM commissions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return {"user_id": row["user_id"], "slots": row["slots"], "where": row["where_link"]}
    return None


def db_load_all_commissions():
    conn = get_db()
    rows = conn.execute("SELECT * FROM commissions").fetchall()
    conn.close()
    return [{"user_id": row["user_id"], "slots": row["slots"], "where": row["where_link"]} for row in rows]


# ============================================================
#  BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Stores when each user's Active role should expire
# Format: { user_id: datetime }
expiry_times = {}

# Track which giveaway messages we've already pinged for (avoid double pinging)
pinged_giveaways = set()

# ============================================================
#  ART TRADE POOL (loaded from SQLite on startup, kept in memory + synced)
# ============================================================

art_trade_pool = {}  # populated in on_ready via db_load_all_entries()

SIZE_CHOICES = ["Headshot", "Fullbody", "Full Scene"]
STYLE_CHOICES = ["Anthro", "Quad", "Human", "Other"]
MEDIUM_CHOICES = ["Digital", "Traditional"]


def entries_match(a, b):
    """Two entries match if EVERY field has at least one overlapping option."""
    if a["user_id"] == b["user_id"]:
        return False

    if not set(a["size"]) & set(b["size"]):
        return False
    if not set(a["style"]) & set(b["style"]):
        return False
    if not set(a["medium"]) & set(b["medium"]):
        return False

    return True


def format_list(values):
    return ", ".join(values)


class MatchConfirmView(discord.ui.View):
    """Buttons shown in the DM asking a user to confirm or decline the match."""

    def __init__(self, entry_a, entry_b):
        super().__init__(timeout=86400)  # 24 hours to respond
        self.entry_a = entry_a
        self.entry_b = entry_b
        self.responses = {}  # user_id -> True/False

    async def handle_response(self, interaction: discord.Interaction, accepted: bool):
        self.responses[interaction.user.id] = accepted

        if not accepted:
            await interaction.response.edit_message(
                content="You declined this match. The other user will be notified.",
                view=None
            )
            other_id = self.entry_b["user_id"] if interaction.user.id == self.entry_a["user_id"] else self.entry_a["user_id"]
            try:
                other_user = await bot.fetch_user(other_id)
                await other_user.send("The other person declined the art trade match. You're still in the pool!")
            except Exception:
                pass
            return

        await interaction.response.edit_message(
            content="✅ You accepted! Waiting to see if the other person accepts too...",
            view=None
        )

        a_id = self.entry_a["user_id"]
        b_id = self.entry_b["user_id"]
        if self.responses.get(a_id) and self.responses.get(b_id):
            await finalize_match(self.entry_a, self.entry_b)

    @discord.ui.button(label="Accept Match", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, False)


class StayOrLeaveView(discord.ui.View):
    """Asks a matched user whether to stay in the pool or be removed."""

    def __init__(self, user_id):
        super().__init__(timeout=86400)
        self.user_id = user_id

    @discord.ui.button(label="Remove me from the pool", style=discord.ButtonStyle.secondary)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        art_trade_pool.pop(self.user_id, None)
        db_remove_entry(self.user_id)
        await interaction.response.edit_message(content="You've been removed from the art trade pool. Good luck with your trade! 🎨", view=None)

    @discord.ui.button(label="Keep me in the pool", style=discord.ButtonStyle.primary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="You'll stay in the pool in case another good match comes along! 🎨", view=None)


async def finalize_match(entry_a, entry_b):
    """Called when both users have accepted the match."""
    try:
        user_a = await bot.fetch_user(entry_a["user_id"])
        user_b = await bot.fetch_user(entry_b["user_id"])

        await user_a.send(
            f"🎉 It's a match! You and **{user_b.display_name}** both accepted the art trade!\n"
            f"Their character: **{entry_b['character']}**\n\n"
            f"Reach out to them to get started! Would you like to stay in the pool for future matches?",
            view=StayOrLeaveView(entry_a["user_id"])
        )
        await user_b.send(
            f"🎉 It's a match! You and **{user_a.display_name}** both accepted the art trade!\n"
            f"Their character: **{entry_a['character']}**\n\n"
            f"Reach out to them to get started! Would you like to stay in the pool for future matches?",
            view=StayOrLeaveView(entry_b["user_id"])
        )
        print(f"🎉 Art trade match finalized: {user_a.display_name} <-> {user_b.display_name}")
    except Exception as e:
        print(f"⚠️ Could not finalize match: {e}")


async def process_art_trade_submission(interaction: discord.Interaction, sizes, styles, mediums, character):
    """Shared logic for finalizing an art trade submission once all selections are made."""
    new_entry = {
        "user_id": interaction.user.id,
        "size": sizes,
        "style": styles,
        "medium": mediums,
        "character": character,
    }

    found_match = None
    for existing_entry in art_trade_pool.values():
        if entries_match(new_entry, existing_entry):
            found_match = existing_entry
            break

    art_trade_pool[interaction.user.id] = new_entry
    db_add_entry(new_entry)  # persist to SQLite

    print(f"🎨 {interaction.user.display_name} submitted art trade request — "
          f"Size: {format_list(sizes)}, Style: {format_list(styles)}, Medium: {format_list(mediums)}, "
          f"Character: {character}")

    if found_match:
        print(f"🎨 Potential match found with user {found_match['user_id']}!")
    else:
        print(f"🎨 No match found yet for {interaction.user.display_name} — added to pool ({len(art_trade_pool)} total in pool)")

    await interaction.response.edit_message(
        content=(
            f"✅ You've been added to the art trade pool!\n"
            f"**Size:** {format_list(sizes)}\n"
            f"**Style:** {format_list(styles)}\n"
            f"**Medium:** {format_list(mediums)}\n"
            f"**Character:** {character}\n\n"
            f"💡 Tip: selecting *all* options for a field means you don't mind what your partner offers there.\n"
            f"I'll DM you if a match is found. Use `/cancel` anytime to withdraw."
        ),
        view=None
    )

    if found_match:
        try:
            user_a = await bot.fetch_user(new_entry["user_id"])
            user_b = await bot.fetch_user(found_match["user_id"])

            await user_a.send(
                f"🎨 A potential art trade match was found!\n"
                f"**{user_b.display_name}** offers: {format_list(found_match['size'])} / "
                f"{format_list(found_match['style'])} / {format_list(found_match['medium'])}\n"
                f"Character: **{found_match['character']}**\n\n"
                f"Do you want to accept this match?",
                view=MatchConfirmView(new_entry, found_match)
            )
            await user_b.send(
                f"🎨 A potential art trade match was found!\n"
                f"**{user_a.display_name}** offers: {format_list(new_entry['size'])} / "
                f"{format_list(new_entry['style'])} / {format_list(new_entry['medium'])}\n"
                f"Character: **{new_entry['character']}**\n\n"
                f"Do you want to accept this match?",
                view=MatchConfirmView(new_entry, found_match)
            )
            print(f"🎨 Potential match found: {user_a.display_name} <-> {user_b.display_name}")
        except Exception as e:
            print(f"⚠️ Could not send match DMs: {e}")


class ArtTradeSelectView(discord.ui.View):
    """Multi-select dropdowns for Size, Style, and Medium, plus a Submit button."""

    def __init__(self, character):
        super().__init__(timeout=300)  # 5 minutes to finish selecting
        self.character = character
        self.selected_sizes = []
        self.selected_styles = []
        self.selected_mediums = []

        self.size_select = discord.ui.Select(
            placeholder="🖼️ Select size(s) you're offering/looking for...",
            min_values=1,
            max_values=len(SIZE_CHOICES),
            options=[discord.SelectOption(label=s) for s in SIZE_CHOICES],
        )
        self.size_select.callback = self.on_size_select
        self.add_item(self.size_select)

        self.style_select = discord.ui.Select(
            placeholder="🎨 Select style(s) you're offering/looking for...",
            min_values=1,
            max_values=len(STYLE_CHOICES),
            options=[discord.SelectOption(label=s) for s in STYLE_CHOICES],
        )
        self.style_select.callback = self.on_style_select
        self.add_item(self.style_select)

        self.medium_select = discord.ui.Select(
            placeholder="✏️ Select medium(s) you're offering/looking for...",
            min_values=1,
            max_values=len(MEDIUM_CHOICES),
            options=[discord.SelectOption(label=s) for s in MEDIUM_CHOICES],
        )
        self.medium_select.callback = self.on_medium_select
        self.add_item(self.medium_select)

    async def on_size_select(self, interaction: discord.Interaction):
        self.selected_sizes = self.size_select.values
        await interaction.response.defer()

    async def on_style_select(self, interaction: discord.Interaction):
        self.selected_styles = self.style_select.values
        await interaction.response.defer()

    async def on_medium_select(self, interaction: discord.Interaction):
        self.selected_mediums = self.medium_select.values
        await interaction.response.defer()

    @discord.ui.button(label="Submit Art Trade Request", style=discord.ButtonStyle.success, emoji="🎨", row=3)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_sizes or not self.selected_styles or not self.selected_mediums:
            await interaction.response.send_message(
                "⚠️ Please select at least one option in each dropdown (Size, Style, Medium) before submitting!",
                ephemeral=True
            )
            return

        await process_art_trade_submission(
            interaction, self.selected_sizes, self.selected_styles, self.selected_mediums, self.character
        )


@bot.tree.command(name="arttrade", description="Submit a request to find an art trade partner")
@app_commands.describe(character="The character you want drawn")
async def arttrade(interaction: discord.Interaction, character: str):
    await interaction.response.send_message(
        "🎨 Pick your Size, Style, and Medium below — you can select **multiple** options in each! "
        "Then hit Submit.\n\n"
        "💡 Tip: selecting *all* options for a field means you're flexible and don't mind what your partner offers there.",
        view=ArtTradeSelectView(character),
        ephemeral=True
    )


@bot.tree.command(name="cancel", description="Withdraw your art trade request from the pool")
async def cancel(interaction: discord.Interaction):
    if interaction.user.id in art_trade_pool:
        del art_trade_pool[interaction.user.id]
        db_remove_entry(interaction.user.id)
        await interaction.response.send_message("You've been removed from the art trade pool.", ephemeral=True)
    else:
        await interaction.response.send_message("You don't have an active art trade request.", ephemeral=True)


# ============================================================
#  COMMISSIONS
# ============================================================

@bot.tree.command(name="listcomm", description="List your open commissions")
@app_commands.describe(
    where="Where to find/order your commissions (link or description)",
    slots="Number of open slots (leave blank for unlimited/until you close it)",
)
async def listcomm(interaction: discord.Interaction, where: str, slots: int = None):
    db_set_commission(interaction.user.id, slots, where)
    slots_text = f"{slots} slot(s)" if slots is not None else "unlimited slots"
    await interaction.response.send_message(
        f"✅ Your commissions are now listed as open with {slots_text}!\nUse `/updatecomm` to change your slot count, or `/closecomm` to remove your listing.",
        ephemeral=True
    )
    print(f"🖌️ {interaction.user.display_name} listed commissions ({slots_text})")


@bot.tree.command(name="updatecomm", description="Update your remaining commission slots")
@app_commands.describe(slots="Your new number of open slots")
async def updatecomm(interaction: discord.Interaction, slots: int):
    existing = db_get_commission(interaction.user.id)
    if not existing:
        await interaction.response.send_message(
            "You don't have an active commission listing. Use `/listcomm` first!",
            ephemeral=True
        )
        return

    db_set_commission(interaction.user.id, slots, existing["where"])
    await interaction.response.send_message(f"✅ Updated! You now have {slots} slot(s) open.", ephemeral=True)
    print(f"🖌️ {interaction.user.display_name} updated commission slots to {slots}")


@bot.tree.command(name="closecomm", description="Remove your commission listing")
async def closecomm(interaction: discord.Interaction):
    existing = db_get_commission(interaction.user.id)
    if not existing:
        await interaction.response.send_message("You don't have an active commission listing.", ephemeral=True)
        return

    db_remove_commission(interaction.user.id)
    await interaction.response.send_message("Your commission listing has been removed.", ephemeral=True)
    print(f"🖌️ {interaction.user.display_name} closed their commission listing")


@bot.tree.command(name="opencomms", description="Get a DM with everyone currently offering open commissions")
async def opencomms(interaction: discord.Interaction):
    all_comms = db_load_all_commissions()

    if not all_comms:
        await interaction.response.send_message("No one currently has open commissions listed.", ephemeral=True)
        return

    lines = []
    for comm in all_comms:
        try:
            user = await bot.fetch_user(comm["user_id"])
            name = user.display_name
        except Exception:
            name = f"User {comm['user_id']}"

        slots_text = f"{comm['slots']} slot(s)" if comm["slots"] is not None else "Unlimited slots"
        lines.append(f"**{name}** — {slots_text}\n{comm['where']}")

    message_text = "🖌️ **Currently Open Commissions**\n\n" + "\n\n".join(lines)

    try:
        await interaction.user.send(message_text)
        await interaction.response.send_message("📬 Sent you a DM with the current list!", ephemeral=True)
    except Exception:
        await interaction.response.send_message(
            "⚠️ I couldn't DM you — please check your privacy settings allow DMs from server members.",
            ephemeral=True
        )


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

    if "Ends:" in embed_text and "Ended:" not in embed_text:
        ping_role = discord.utils.get(message.guild.roles, name=GIVEAWAY_PING_ROLE)
        if ping_role:
            pinged_giveaways.add(message.id)
            await message.channel.send(ping_role.mention)
            print(f"🎉 Pinged @{GIVEAWAY_PING_ROLE} for new giveaway in #{message.channel.name}")


# ============================================================
#  STARTUP ACTIVITY CHECK
# ============================================================

async def startup_activity_check():
    await bot.wait_until_ready()
    print(f"🔍 Running startup activity check...")

    for guild in bot.guilds:
        active_role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)
        if not active_role:
            print(f"⚠️ Role '{ACTIVE_ROLE_NAME}' not found in {guild.name}")
            continue

        valid_channels = [
            c for c in guild.text_channels
            if c.name not in IGNORED_CHANNELS
            and c.name != GIVEAWAY_CHANNEL
        ]

        active_members = [m for m in guild.members if active_role in m.roles]
        print(f"📋 Checking {len(active_members)} members with Active role...")

        cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVE_DURATION_DAYS)

        # Build a map of user_id -> most recent post time by scanning each
        # channel ONCE (newest messages first), instead of per-member per-channel.
        # This avoids missing posts in busy channels that have 500+ messages
        # since the cutoff date.
        last_post_map = {}

        for channel in valid_channels:
            try:
                async for msg in channel.history(limit=None, after=cutoff, oldest_first=False):
                    if msg.author.bot:
                        continue
                    existing = last_post_map.get(msg.author.id)
                    if existing is None or msg.created_at > existing:
                        last_post_map[msg.author.id] = msg.created_at
            except Exception as e:
                print(f"⚠️ Could not scan #{channel.name}: {e}")
                continue

        for member in active_members:
            if member.bot:
                continue

            last_post = last_post_map.get(member.id)

            if last_post:
                expiry = last_post + timedelta(days=ACTIVE_DURATION_DAYS)
                expiry_times[member.id] = expiry
                print(f"✅ {member.display_name} — last post {last_post.strftime('%Y-%m-%d')}, expires {expiry.strftime('%Y-%m-%d')}")
            else:
                try:
                    await member.remove_roles(active_role)
                    print(f"⏰ Removed '{ACTIVE_ROLE_NAME}' from {member.display_name} (no posts in 30 days)")
                except Exception as e:
                    print(f"⚠️ Could not remove role from {member.display_name}: {e}")

        # Also check anyone who posted recently but is MISSING the Active role
        # (e.g. wrongly removed by a previous buggy run) and restore it
        active_member_ids = {m.id for m in active_members}
        for user_id, last_post in last_post_map.items():
            if user_id in active_member_ids:
                continue  # already handled above

            member = guild.get_member(user_id)
            if not member or member.bot:
                continue

            try:
                await member.add_roles(active_role)
                expiry = last_post + timedelta(days=ACTIVE_DURATION_DAYS)
                expiry_times[member.id] = expiry
                print(f"🔧 Restored 'Active' to {member.display_name} — last post {last_post.strftime('%Y-%m-%d')} (was missing the role)")
            except Exception as e:
                print(f"⚠️ Could not restore role for {member.display_name}: {e}")

    print(f"✅ Startup activity check complete!")


# ============================================================
#  EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print(f"📋 Watching for activity | Role: '{ACTIVE_ROLE_NAME}' | Window: {ACTIVE_DURATION_DAYS} days")

    # Load persisted art trade pool from SQLite
    global art_trade_pool
    art_trade_pool = db_load_all_entries()
    print(f"🎨 Loaded {len(art_trade_pool)} art trade entries from database")

    try:
        synced = await bot.tree.sync()
        print(f"🔄 Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"⚠️ Failed to sync slash commands: {e}")

    bot.loop.create_task(check_expirations())
    bot.loop.create_task(startup_activity_check())


@bot.event
async def on_message(message):
    if message.guild is None:
        return

    # --------------------------------------------------------
    #  GIVEAWAY CHANNEL
    # --------------------------------------------------------
    if message.channel.name == GIVEAWAY_CHANNEL:

        if message.author == bot.user:
            return

        if message.author.bot:
            await maybe_ping_giveaway(message)
            return

        async def delayed_delete(msg):
            await asyncio.sleep(GIVEAWAY_DELETE_SECONDS)
            try:
                await msg.delete()
                print(f"🗑️ Silently deleted message from {msg.author.display_name} in #{msg.channel.name}")
            except Exception as e:
                print(f"⚠️ Could not delete message: {e}")

        asyncio.ensure_future(delayed_delete(message))
        return

    if message.author.bot:
        return

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


@bot.event
async def on_message_edit(before, after):
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
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        to_remove = [uid for uid, exp in expiry_times.items() if now >= exp]

        for user_id in to_remove:
            del expiry_times[user_id]
            for guild in bot.guilds:
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

init_db()
bot.run(BOT_TOKEN)
