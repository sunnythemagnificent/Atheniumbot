import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import re
import sqlite3
import aiohttp
import html
import xml.etree.ElementTree as ET
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

# Food Club settings
FOOD_CLUB_CHANNEL = "🥕︱food-club"          # Channel name without #
FOOD_CLUB_REDDIT_USER = "nsheng"           # Reddit user who comments the daily outlook
FOOD_CLUB_CHECK_INTERVAL_HOURS = 3         # How often to re-check if today's thread/comment isn't up yet

BOT_MOD_ROLES = ["Moderator", "Admin", "Coordinator"]   # Role names allowed to use admin commands like /foodclubreset

# --- Activity tracker (mod-only page on the website) ---
ACTIVITY_SYNC_URL = os.environ.get("ACTIVITY_SYNC_URL", "https://mods.athenaeumarchive.com/activity_sync.php")
ACTIVITY_SYNC_SECRET = os.environ.get("ACTIVITY_SYNC_SECRET")  # shared secret, set this on Railway
ACTIVITY_SYNC_INTERVAL_HOURS = 1
MESSAGE_LOG_RETENTION_DAYS = 185  # needs to cover the 180-day threshold baseline window, plus a small buffer

# --- Strike system (Discord-only, mod commands) ---
TEMP_STRIKE_DURATION_DAYS = 45       # how long a temporary "bee sting" strike lasts before fading
STRIKE_ALERT_THRESHOLD = 3           # active strikes (temp + permanent) that triggers a mod alert
STRIKE_ALERT_CHANNEL = "🔴︱mod-alerts"  # channel name (no #) where strike/purge/violation alerts post

# --- Minor violation escalation ladder ---
# Level 1 -> 30 day cooldown. Re-triggered inside that window -> Level 2 -> 90 day
# cooldown. Re-triggered inside THAT window -> flagged for manual ban (bot never
# bans automatically). If a cooldown fully expires with no re-trigger, the next
# violation starts fresh back at Level 1.
MINOR_VIOLATION_LEVEL1_COOLDOWN_DAYS = 30
MINOR_VIOLATION_LEVEL2_COOLDOWN_DAYS = 90  # ~3 months

# --- Adaptive activity thresholds ---
# Rather than fixed guessed numbers, the "Really Active" / "Active" cutoffs
# are recalculated from actual server-wide posting data every 30 days, using
# a 6-month trailing window. Slow-moving on purpose — see conversation notes
# on why a fast-reacting baseline could mask a real decline.
THRESHOLD_BASELINE_WINDOW_DAYS = 180
THRESHOLD_RECALC_INTERVAL_DAYS = 30
THRESHOLD_HIGH_PERCENTILE = 0.85   # top 15% of posters -> "Really Active" cutoff
THRESHOLD_MEDIUM_PERCENTILE = 0.60  # top 40% of posters -> "Active" cutoff
THRESHOLD_MIN_SAMPLE_SIZE = 10      # don't recalculate off too small a sample
DEFAULT_HIGH_THRESHOLD = 40.0        # grounded in real member data (was 150 — see conversation notes)
DEFAULT_MEDIUM_THRESHOLD = 10.0      # was 80

# --- Inactivity purge notifications ---
# Mods still remove people manually — this just tells them when someone
# crosses the threshold, instead of relying on someone remembering to check.
# Server boosters are fully immune as long as they're currently boosting.
PURGE_THRESHOLD_DAYS = 180          # ~6 months of total silence
PURGE_HIATUS_THRESHOLD_DAYS = 360   # ~12 months if flagged as on hiatus
PURGE_CHECK_INTERVAL_HOURS = 24
HIATUS_LIST_URL = os.environ.get("HIATUS_LIST_URL", "https://mods.athenaeumarchive.com/hiatus_list.php")

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
    # Make sure the folders exist (in case the volume isn't mounted yet)
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS food_club_status (
            date TEXT PRIMARY KEY,
            outlook TEXT,
            pinged INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_name TEXT NOT NULL,
            posted_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_log_user ON message_log(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_log_time ON message_log(posted_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strikes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            strike_type TEXT NOT NULL,
            reason TEXT NOT NULL,
            issued_by TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strikes_user ON strikes(user_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS minor_violation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            level INTEGER NOT NULL,
            reason TEXT NOT NULL,
            issued_by TEXT NOT NULL,
            issued_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_minor_violation_user ON minor_violation_log(user_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_thresholds (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            high_threshold REAL NOT NULL,
            medium_threshold REAL NOT NULL,
            sample_size INTEGER NOT NULL,
            computed_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS member_last_seen (
            user_id INTEGER PRIMARY KEY,
            last_seen_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS purge_flags (
            user_id INTEGER PRIMARY KEY,
            flagged_at TEXT NOT NULL,
            reason TEXT NOT NULL
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


# --- Food Club helpers ---

def db_get_food_club_status(date_str):
    conn = get_db()
    row = conn.execute("SELECT * FROM food_club_status WHERE date = ?", (date_str,)).fetchone()
    conn.close()
    if row:
        return {"date": row["date"], "outlook": row["outlook"], "pinged": bool(row["pinged"])}
    return None


def db_set_food_club_status(date_str, outlook, pinged):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO food_club_status (date, outlook, pinged)
        VALUES (?, ?, ?)
    """, (date_str, outlook, int(pinged)))
    conn.commit()
    conn.close()


def db_delete_food_club_status(date_str):
    conn = get_db()
    conn.execute("DELETE FROM food_club_status WHERE date = ?", (date_str,))
    conn.commit()
    conn.close()


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
#  FAIR TRADE CALCULATOR
# ============================================================

def parse_items(text):
    """
    Parse item lines into a list of (name, total_value, is_priority, display) tuples.
    Accepts two formats:
      - 'Item Name:Value'        e.g. 'Liquid Glass Filter:4'
      - 'Item Name - Value Caps' e.g. 'Liquid Glass Filter - 4 Caps'
    Both formats support:
      - Ranges, e.g. 'Item:1-2' or 'Item - 1-2 Caps' — the midpoint is used for math,
        the original range is kept for display.
      - A '*' prefix on the name to mark it as a must-keep priority item.
      - A quantity multiplier, e.g. 'Item - 1.5 Caps (x15)' or 'Item - 1.5 Caps x15'
        — multiplies the per-unit value by the quantity for the total.
    """
    items = []
    errors = []
    if not text or not text.strip():
        return items, errors

    for chunk in re.split(r"[,\n]+", text):
        chunk = chunk.strip()
        if not chunk:
            continue

        name = None
        value_str = None

        if ":" in chunk:
            name, _, value_str = chunk.rpartition(":")
        elif " - " in chunk:
            name, _, value_str = chunk.rpartition(" - ")
        else:
            errors.append(chunk)
            continue

        name = name.strip()
        value_str = value_str.strip()

        is_priority = name.startswith("*")
        if is_priority:
            name = name[1:].strip()

        # Pull out the numeric value or range, ignoring trailing words like "Caps"/"Cap"
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?", value_str)
        if not name or not match:
            errors.append(chunk)
            continue

        low = float(match.group(1))
        high = float(match.group(2)) if match.group(2) else low
        if low > high:
            low, high = high, low
        unit_value = (low + high) / 2

        # Look for a quantity multiplier AFTER the value, e.g. "(x15)" or "x15"
        quantity = 1
        qty_match = re.search(r"x\s*(\d+)", value_str[match.end():], re.IGNORECASE)
        if qty_match:
            quantity = int(qty_match.group(1))

        total_value = unit_value * quantity

        unit_display = f"{low:g}" if low == high else f"{low:g}-{high:g}"
        if quantity > 1:
            display = f"{unit_display} (x{quantity}) = {total_value:g}"
        else:
            display = unit_display

        items.append((name, total_value, is_priority, display))

    return items, errors


def build_value_clusters(items):
    """Groups items sharing the same value together, e.g. two items both worth 2
    become one cluster so they can be suggested as 'Item C or Item D' instead
    of as separate, redundant combo options."""
    clusters = {}
    order = []
    for it in items:
        key = round(it[1], 4)
        if key not in clusters:
            clusters[key] = []
            order.append(key)
        clusters[key].append(it)
    return [(key, clusters[key]) for key in order]


def format_cluster_group(cluster):
    value, item_list = cluster
    return " or ".join(it[0] for it in item_list)


def format_combo_side(chosen_clusters):
    return " + ".join(format_cluster_group(c) for c in chosen_clusters)


def find_matching_combos(side_a_items, side_b_items, max_subset_size=3, max_results=4, tolerance_ratio=0.15):
    """
    Finds smaller groupings of items from each side whose combined values are
    close to each other — e.g. 'your Item A + Item B ≈ their Item G' — rather
    than only looking at the overall trade balance.
    Capped in scope to keep this fast even with a handful of items per side.
    """
    from itertools import combinations

    clusters_a = build_value_clusters(side_a_items)
    clusters_b = build_value_clusters(side_b_items)

    # Safety cap — reduce subset depth if there are a lot of distinct clusters
    if len(clusters_a) > 10 or len(clusters_b) > 10:
        max_subset_size = min(max_subset_size, 2)

    def gen_subsets(clusters, max_size):
        n = len(clusters)
        subsets = []
        for size in range(1, min(max_size, n) + 1):
            for combo_idx in combinations(range(n), size):
                chosen = [clusters[i] for i in combo_idx]
                total = sum(val for val, _ in chosen)
                subsets.append((chosen, total))
        return subsets

    subsets_a = gen_subsets(clusters_a, max_subset_size)
    subsets_b = gen_subsets(clusters_b, max_subset_size)

    matches = []
    for subset_a, total_a in subsets_a:
        for subset_b, total_b in subsets_b:
            diff = abs(total_a - total_b)
            tolerance = max(total_a, total_b, 1) * tolerance_ratio
            if diff <= tolerance:
                matches.append((subset_a, subset_b, total_a, total_b, diff))

    # Best (closest) matches first, preferring simpler combos when tied
    matches.sort(key=lambda m: (round(m[4], 2), len(m[0]) + len(m[1])))

    seen = set()
    unique_matches = []
    for subset_a, subset_b, total_a, total_b, diff in matches:
        signature = (
            frozenset(val for val, _ in subset_a),
            frozenset(val for val, _ in subset_b),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique_matches.append((subset_a, subset_b, total_a, total_b, diff))
        if len(unique_matches) >= max_results:
            break

    return unique_matches


def find_best_removal_subsets(items, target_diff, max_options=3):
    """
    Find up to `max_options` distinct subsets of `items` whose combined value is
    closest to target_diff. Removing any one of these subsets would balance the trade.
    Priority items (is_priority=True) are NEVER included in the removable pool.
    Results are ranked by closeness to target, then by fewest items removed.
    Brute force — fine for small item counts (capped at 15 for safety).
    """
    removable_items = [item for item in items if not item[2]]

    if not removable_items or len(removable_items) > 15:
        return []

    all_subsets = []
    n = len(removable_items)
    for mask in range(1, 1 << n):
        subset = [removable_items[i] for i in range(n) if mask & (1 << i)]
        subset_value = sum(v for _, v, _, _ in subset)
        diff_from_target = abs(subset_value - target_diff)
        all_subsets.append((subset, diff_from_target))

    # Sort by closeness to target first, then prefer fewer items removed
    all_subsets.sort(key=lambda x: (round(x[1], 2), len(x[0])))

    # Only keep subsets with genuinely distinct item sets (dedupe by item names)
    seen_signatures = set()
    unique_options = []
    for subset, diff_from_target in all_subsets:
        signature = frozenset(n for n, _, _, _ in subset)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        unique_options.append((subset, diff_from_target))
        if len(unique_options) >= max_options:
            break

    return unique_options


def format_items(items):
    return ", ".join(f"⭐{n} ({d})" if p else f"{n} ({d})" for n, v, p, d in items)


class FairTradeModal(discord.ui.Modal, title="Fair Trade Calculator"):
    your_items = discord.ui.TextInput(
        label="Your items",
        style=discord.TextStyle.paragraph,
        placeholder="Liquid Glass Filter:4\n*Subtle Blush - 1-2 Caps",
        required=True,
    )
    their_items = discord.ui.TextInput(
        label="Their items",
        style=discord.TextStyle.paragraph,
        placeholder="Pfish Trinket - 3 Caps\nOversized Witch Hat:2-3",
        required=True,
    )

    def __init__(self, include_combos=False):
        super().__init__()
        self.include_combos = include_combos

    async def on_submit(self, interaction: discord.Interaction):
        your_list, your_errors = parse_items(self.your_items.value)
        their_list, their_errors = parse_items(self.their_items.value)

        if your_errors or their_errors:
            bad = your_errors + their_errors
            await interaction.response.send_message(
                f"⚠️ Couldn't parse these entries (use `Name:Value` or `Name - Value Caps`): {', '.join(bad)}",
                ephemeral=True
            )
            return

        if not your_list or not their_list:
            await interaction.response.send_message(
                "⚠️ Please list at least one item on each side.",
                ephemeral=True
            )
            return

        your_total = sum(v for _, v, _, _ in your_list)
        their_total = sum(v for _, v, _, _ in their_list)
        diff = round(your_total - their_total, 2)

        lines = [
            f"**Your side:** {format_items(your_list)} — Total: **{your_total:g}**",
            f"**Their side:** {format_items(their_list)} — Total: **{their_total:g}**",
            "",
        ]
        if any(p for _, _, p, _ in your_list) or any(p for _, _, p, _ in their_list):
            lines.append("⭐ = marked as a must-keep priority item (won't be suggested for removal)")
        if any("-" in d for _, _, _, d in your_list) or any("-" in d for _, _, _, d in their_list):
            lines.append("*(Ranged items use their midpoint value for calculations)*")
        if lines[-1] != "":
            lines.append("")

        if diff == 0:
            lines.append("✅ This trade is perfectly fair!")
        else:
            # "your"/"their" as possessive determiners for correct grammar
            heavier_owner = "your" if diff > 0 else "their"
            heavier_items = your_list if diff > 0 else their_list
            target = abs(diff)

            lines.append(f"⚖️ **{heavier_owner.capitalize()}** side is worth **{target:g}** more.")

            options = find_best_removal_subsets(heavier_items, target, max_options=3)
            if options:
                if len(options) == 1:
                    lines.append(f"\n💡 To balance it out, consider removing:")
                else:
                    lines.append(f"\n💡 A few ways to balance it out — remove:")

                for i, (subset, subset_diff) in enumerate(options, start=1):
                    subset_names = ", ".join(f"{n} ({d})" for n, v, _, d in subset)
                    subset_value = sum(v for _, v, _, _ in subset)
                    gap_note = ""
                    if round(subset_diff, 2) != 0:
                        gap_note = f" *(leaves a small gap of ~{round(subset_diff, 2):g})*"

                    prefix = f"**Option {i}:**" if len(options) > 1 else "•"
                    lines.append(f"{prefix} **{subset_names}** (worth {subset_value:g}) from {heavier_owner} side{gap_note}")
            else:
                removable_count = len([i for i in heavier_items if not i[2]])
                if removable_count == 0:
                    lines.append(
                        f"\n💡 All items on {heavier_owner} side are marked as priority, "
                        f"so consider adding roughly **{target:g}** worth of items to the lighter side instead."
                    )
                else:
                    lines.append(f"\n💡 Consider adding roughly **{target:g}** worth of items to the lighter side instead.")

        # Suggest smaller item groupings that could be traded against each other,
        # separate from the overall balance check above — only if opted in
        if self.include_combos:
            combos = find_matching_combos(your_list, their_list)
            if combos:
                lines.append("\n🔀 **Possible ways to arrange the trade:**")
                for subset_a, subset_b, total_a, total_b, combo_diff in combos:
                    your_side_text = format_combo_side(subset_a)
                    their_side_text = format_combo_side(subset_b)
                    gap_note = "" if round(combo_diff, 2) == 0 else f" *(off by ~{round(combo_diff, 2):g})*"
                    lines.append(f"• Your **{your_side_text}** ({total_a:g}) ≈ their **{their_side_text}** ({total_b:g}){gap_note}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class FairTradeStartView(discord.ui.View):
    """Shown before the modal so we have room for full instructions (modal labels cap at 45 characters)."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Open Trade Form", style=discord.ButtonStyle.primary, emoji="📝")
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FairTradeModal(include_combos=False))

    @discord.ui.button(label="Open Form + Suggest Combos", style=discord.ButtonStyle.secondary, emoji="🔀")
    async def open_form_with_combos(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FairTradeModal(include_combos=True))


@bot.tree.command(name="fairtrade", description="Check if a Neocash trade is fair and get suggestions to balance it")
async def fairtrade(interaction: discord.Interaction):
    instructions = (
        "🎨 **Fair Trade Calculator**\n\n"
        "List each item on its own **line**, or separated by **commas**. Both work!\n\n"
        "**Accepted formats:**\n"
        "• `Item Name:Value` — e.g. `Liquid Glass Filter:4`\n"
        "• `Item Name - Value Caps` — e.g. `Liquid Glass Filter - 4 Caps`\n\n"
        "**Value can be a range too:**\n"
        "• `Subtle Blush:1-2` or `Subtle Blush - 1-2 Caps` *(the midpoint is used for the math)*\n\n"
        "**Quantities:** add `(x15)` after the value for multiples —\n"
        "`Gift Box Capsule:1.5 (x15)` = 22.5 total\n\n"
        "**Priority items:** put a `*` before the name to mark it as a must-keep —\n"
        "`*Rare Item:5` — it will never be suggested for removal.\n\n"
        "**Two ways to open the form:**\n"
        "📝 **Open Trade Form** — just checks if the trade is fair overall\n"
        "🔀 **Open Form + Suggest Combos** — also suggests smaller item groupings "
        "that could be swapped against each other (e.g. \"your A + B ≈ their C\")"
    )
    await interaction.response.send_message(instructions, view=FairTradeStartView(), ephemeral=True)


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

def has_sufficient_message_history() -> bool:
    """Checks whether message_log has been running long enough (a full
    ACTIVE_DURATION_DAYS window) to safely replace the old full-channel-scan
    with a fast local lookup. Returns False until then, so the bot falls
    back to the slower-but-reliable Discord history scan in the meantime."""
    conn = get_db()
    row = conn.execute("SELECT MIN(posted_at) as oldest FROM message_log").fetchone()
    conn.close()

    if row is None or row["oldest"] is None:
        return False

    oldest = datetime.fromisoformat(row["oldest"])
    return (datetime.now(timezone.utc) - oldest).days >= ACTIVE_DURATION_DAYS


async def startup_activity_check():
    await bot.wait_until_ready()
    print(f"🔍 Running startup activity check...")

    use_local_data = has_sufficient_message_history()
    if use_local_data:
        print("📊 message_log has enough history — using the fast local lookup instead of scanning Discord")
    else:
        print("📊 message_log doesn't have a full 30-day history yet — falling back to the slower full channel scan for now")

    for guild in bot.guilds:
        # Force a full member list load — the automatic chunking Discord
        # does in the background isn't always finished by the time this
        # runs, which can otherwise make guild.members look empty.
        if not guild.chunked:
            await guild.chunk()

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
        last_post_map = {}

        if use_local_data:
            # Fast path: message_log already tracks this continuously and
            # respects the same channel exclusions (IGNORED_CHANNELS and the
            # giveaway channel never get logged there in the first place).
            conn = get_db()
            rows = conn.execute(
                "SELECT user_id, MAX(posted_at) as last_post FROM message_log WHERE posted_at >= ? GROUP BY user_id",
                (cutoff.isoformat(),)
            ).fetchall()
            conn.close()
            for row in rows:
                last_post_map[row["user_id"]] = datetime.fromisoformat(row["last_post"])
        else:
            # Slow fallback: scan full channel history. Only used until
            # message_log has accumulated a full 30-day window on its own.
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

    # Fully load every guild's member list BEFORE starting any background
    # tasks — otherwise multiple tasks starting at once can each race to
    # chunk the same guild independently, and some can end up running
    # against an incomplete member list.
    for guild in bot.guilds:
        if not guild.chunked:
            print(f"📥 Loading full member list for {guild.name}...")
            await guild.chunk()
            print(f"✅ {guild.name} fully loaded ({len(guild.members)} members)")

    bot.loop.create_task(check_expirations())
    bot.loop.create_task(startup_activity_check())
    bot.loop.create_task(food_club_check_loop())
    bot.loop.create_task(activity_sync_loop())
    bot.loop.create_task(prune_message_log_loop())
    bot.loop.create_task(threshold_recalc_loop())
    bot.loop.create_task(purge_check_loop())


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
    #  ACTIVITY LOGGING (for the mod-only activity tracker)
    # --------------------------------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO message_log (user_id, channel_name, posted_at) VALUES (?, ?, ?)",
        (message.author.id, message.channel.name, now_iso)
    )
    # Permanent last-seen record — never pruned, so the 6-month purge check
    # still works correctly even past the rolling activity window.
    conn.execute("""
        INSERT INTO member_last_seen (user_id, last_seen_at) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
    """, (message.author.id, now_iso))
    # If they were flagged for the inactivity purge, they've clearly come
    # back — clear it so mods aren't looking at a stale flag.
    conn.execute("DELETE FROM purge_flags WHERE user_id = ?", (message.author.id,))
    conn.commit()
    conn.close()

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
#  FOOD CLUB OUTLOOK CHECKER
# ============================================================

ATOM_NS = "{http://www.w3.org/2005/Atom}"

REDDIT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


async def fetch_rss(url, retries=2, backoff_seconds=8):
    """
    Fetch and parse an Atom/RSS feed. Returns the parsed XML root, or None on failure.
    Retries on 429 (rate limited) with a short backoff, and falls back to
    old.reddit.com if www.reddit.com is being blocked.
    """
    urls_to_try = [url]
    if "www.reddit.com" in url:
        urls_to_try.append(url.replace("www.reddit.com", "old.reddit.com"))

    for attempt_url in urls_to_try:
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        attempt_url, headers=REDDIT_HEADERS, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            try:
                                return ET.fromstring(text)
                            except ET.ParseError as e:
                                print(f"⚠️ Food Club: couldn't parse RSS feed — {e}")
                                return None
                        elif resp.status == 429:
                            print(f"⚠️ Food Club: rate limited (429) on {attempt_url}, attempt {attempt + 1}/{retries + 1}")
                            if attempt < retries:
                                await asyncio.sleep(backoff_seconds)
                        else:
                            print(f"⚠️ Food Club: RSS returned status {resp.status} for {attempt_url}")
                            break  # non-429 error, no point retrying this URL
            except Exception as e:
                print(f"⚠️ Food Club: couldn't reach {attempt_url} — {e}")
                break

    return None


async def find_todays_food_club_thread():
    """
    Search r/neopets for today's AutoModerator 'Food Club Bets' thread.
    Returns the thread's URL, or None if it hasn't been posted yet.
    """
    from urllib.parse import quote
    query = quote('title:"Food Club Bets"')
    search_url = f"https://www.reddit.com/r/neopets/search.rss?q={query}&restrict_sr=on&sort=new&limit=5"

    root = await fetch_rss(search_url)
    if root is None:
        return None

    now = datetime.now(timezone.utc)
    today_labels = {now.strftime("%B %-d, %Y").lower(), now.strftime("%B %d, %Y").lower()}

    for entry in root.findall(f"{ATOM_NS}entry"):
        title_el = entry.find(f"{ATOM_NS}title")
        title = (title_el.text or "") if title_el is not None else ""
        title_lower = title.lower()

        if "food club bets" not in title_lower:
            continue
        if not any(label in title_lower for label in today_labels):
            continue

        link_el = entry.find(f"{ATOM_NS}link")
        if link_el is not None:
            return link_el.get("href")

    return None


async def fetch_food_club_outlook():
    """
    Find today's AutoModerator Food Club Bets thread, then look for u/nsheng's
    comment inside it containing the outlook.
    Returns (outlook_text, comment_url, is_skip_day) if found, or
    (None, None, False) if the thread or comment isn't up yet, or
    something went wrong.
    """
    thread_url = await find_todays_food_club_thread()
    if not thread_url:
        return None, None, False

    await asyncio.sleep(3)  # brief pause between requests to be gentle on Reddit's rate limits

    comments_rss_url = thread_url if thread_url.endswith("/") else thread_url + "/"
    comments_rss_url += ".rss"

    root = await fetch_rss(comments_rss_url)
    if root is None:
        return None, None, False

    for entry in root.findall(f"{ATOM_NS}entry"):
        author_el = entry.find(f"{ATOM_NS}author/{ATOM_NS}name")
        author = (author_el.text or "") if author_el is not None else ""

        if FOOD_CLUB_REDDIT_USER.lower() not in author.lower():
            continue

        content_el = entry.find(f"{ATOM_NS}content")
        content_html = (content_el.text or "") if content_el is not None else ""

        # Check for a "skipping this round" mention anywhere in the comment —
        # strip HTML tags first so a phrase split across tags still matches
        plain_text = re.sub(r"<[^>]+>", " ", content_html)
        is_skip_day = bool(re.search(r"sets?\W+(?:are\W+)?skipping", plain_text, re.IGNORECASE))

        match = re.search(r"outlook for this round:\s*([^<\n]+)", content_html, re.IGNORECASE)
        if not match and not is_skip_day:
            continue

        outlook_text = html.unescape(match.group(1).strip()) if match else None

        link_el = entry.find(f"{ATOM_NS}link")
        comment_url = link_el.get("href") if link_el is not None else thread_url

        return outlook_text, comment_url, is_skip_day

    print(f"🥕 Food Club: found today's thread but no matching comment from u/{FOOD_CLUB_REDDIT_USER} yet")
    return None, None, False


async def run_food_club_check():
    """
    Runs a single Food Club check cycle: fetch and post today's outlook.
    Posts every day regardless of risk/return level — just the outlook text,
    no bet links, no pings. Returns a short summary string describing what
    happened. Used by both the background loop and the /foodclubreset command.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = db_get_food_club_status(today_str)

    if existing is not None:
        msg = f"Already checked {today_str} (outlook was: '{existing['outlook']}')"
        print(f"🥕 Food Club: {msg} — waiting until tomorrow")
        return msg

    outlook_text, post_url, is_skip_day = await fetch_food_club_outlook()

    if is_skip_day:
        print(f"🥕 Food Club: nsheng mentioned all sets skipping today")
        posted_anywhere = False
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=FOOD_CLUB_CHANNEL)
            if channel:
                try:
                    await channel.send("🥕 nsheng mentioned all sets are skipping this round.")
                    posted_anywhere = True
                except Exception as e:
                    print(f"⚠️ Food Club: couldn't send message — {e}")

        db_set_food_club_status(today_str, "Skip day — all sets skipping", False)
        return "Skip day detected (nsheng mentioned all sets skipping)" + (" — posted" if posted_anywhere else " — couldn't find channel")

    elif outlook_text:
        # Strip any trailing period/exclamation from nsheng's text so our
        # own formatting doesn't collide with his punctuation
        outlook_display = outlook_text.rstrip(" .!")

        posted_anywhere = False
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=FOOD_CLUB_CHANNEL)
            if channel:
                try:
                    await channel.send(f"🥕 Today's Food Club outlook: **{outlook_display}**")
                    posted_anywhere = True
                    print(f"🥕 Posted — outlook: {outlook_text}")
                except Exception as e:
                    print(f"⚠️ Food Club: couldn't send message — {e}")

        db_set_food_club_status(today_str, outlook_text, False)
        if posted_anywhere:
            return f"Posted! Outlook: '{outlook_text}'"
        else:
            return f"Found outlook ('{outlook_text}') but couldn't find the channel to post in"
    else:
        msg = f"No post found for {today_str} yet"
        print(f"🥕 Food Club: {msg}, will check again in {FOOD_CLUB_CHECK_INTERVAL_HOURS}h")
        return msg


async def food_club_check_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        await run_food_club_check()
        await asyncio.sleep(FOOD_CLUB_CHECK_INTERVAL_HOURS * 3600)


# ============================================================
#  ACTIVITY TRACKER — computes tiers and pushes them to the
#  mod-only page on the website. Nothing here is visible to
#  regular members; it's purely a check-in tool for staff.
# ============================================================

def get_current_thresholds():
    """Returns (high_threshold, medium_threshold, computed_at) — falls back
    to the starting defaults until enough real data exists to compute from."""
    conn = get_db()
    row = conn.execute("SELECT * FROM activity_thresholds WHERE id = 1").fetchone()
    conn.close()

    if row is None:
        return (DEFAULT_HIGH_THRESHOLD, DEFAULT_MEDIUM_THRESHOLD, None)
    return (row["high_threshold"], row["medium_threshold"], row["computed_at"])


def recalculate_activity_thresholds():
    """Looks at every member's actual posting rate over the trailing 6-month
    window and derives the Really Active / Active cutoffs from real
    percentiles, instead of a fixed guess. Runs monthly — slow on purpose,
    so it can't quietly absorb a real decline the way a fast-reacting
    baseline could."""
    conn = get_db()
    window_start = (datetime.now(timezone.utc) - timedelta(days=THRESHOLD_BASELINE_WINDOW_DAYS)).isoformat()

    rows = conn.execute(
        "SELECT user_id, COUNT(*) as cnt FROM message_log WHERE posted_at >= ? GROUP BY user_id",
        (window_start,)
    ).fetchall()

    daily_rates = sorted((r["cnt"] / THRESHOLD_BASELINE_WINDOW_DAYS) for r in rows)
    sample_size = len(daily_rates)

    if sample_size < THRESHOLD_MIN_SAMPLE_SIZE:
        conn.close()
        print(f"📊 Threshold recalc skipped — only {sample_size} members with data, need {THRESHOLD_MIN_SAMPLE_SIZE}")
        return

    high_idx = min(int(sample_size * THRESHOLD_HIGH_PERCENTILE), sample_size - 1)
    medium_idx = min(int(sample_size * THRESHOLD_MEDIUM_PERCENTILE), sample_size - 1)
    high_threshold = round(daily_rates[high_idx], 1)
    medium_threshold = round(daily_rates[medium_idx], 1)

    # Sanity guard: high should never end up below medium (can happen with
    # small/lopsided samples) — if so, just keep the previous thresholds.
    if high_threshold <= medium_threshold:
        conn.close()
        print(f"📊 Threshold recalc skipped — computed high ({high_threshold}) <= medium ({medium_threshold}), keeping previous values")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO activity_thresholds (id, high_threshold, medium_threshold, sample_size, computed_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            high_threshold = excluded.high_threshold,
            medium_threshold = excluded.medium_threshold,
            sample_size = excluded.sample_size,
            computed_at = excluded.computed_at
    """, (high_threshold, medium_threshold, sample_size, now_iso))
    conn.commit()
    conn.close()
    print(f"📊 Recalculated activity thresholds from {sample_size} members: High={high_threshold}/day, Medium={medium_threshold}/day")


async def threshold_recalc_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        recalculate_activity_thresholds()
        await asyncio.sleep(THRESHOLD_RECALC_INTERVAL_DAYS * 24 * 3600)


# ============================================================
#  INACTIVITY PURGE NOTIFICATIONS
#  Flags members who've been fully silent for 6+ months (12+ if on
#  hiatus) so mods know to review them — never removes anyone
#  automatically. Server boosters are fully immune while boosting.
# ============================================================

async def fetch_hiatus_discord_ids() -> set:
    """Pulls the current hiatus list from the website (set via mod_activity.php),
    so this stays the single source of truth rather than a separate bot-side list."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                HIATUS_LIST_URL,
                headers={"X-Sync-Secret": ACTIVITY_SYNC_SECRET},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                return set(data.get("discord_ids", []))
        except Exception as e:
            print(f"⚠️ Could not fetch hiatus list, treating nobody as on hiatus this run: {e}")
            return set()


async def run_purge_check():
    if not ACTIVITY_SYNC_SECRET:
        print("⚠️ ACTIVITY_SYNC_SECRET not set — skipping purge check")
        return

    hiatus_ids = await fetch_hiatus_discord_ids()
    now = datetime.now(timezone.utc)
    conn = get_db()

    for guild in bot.guilds:
        if not guild.chunked:
            await guild.chunk()

        alert_channel = discord.utils.get(guild.text_channels, name=STRIKE_ALERT_CHANNEL)

        for member in guild.members:
            if member.bot:
                continue

            # Server boosters are fully immune while currently boosting.
            if member.premium_since is not None:
                continue

            row = conn.execute(
                "SELECT last_seen_at FROM member_last_seen WHERE user_id = ?",
                (member.id,)
            ).fetchone()

            if row:
                last_activity = datetime.fromisoformat(row["last_seen_at"])
            else:
                # Never posted at all since we started tracking — fall back
                # to their join date as the reference point.
                last_activity = member.joined_at or now

            on_hiatus = str(member.id) in hiatus_ids
            threshold_days = PURGE_HIATUS_THRESHOLD_DAYS if on_hiatus else PURGE_THRESHOLD_DAYS
            days_silent = (now - last_activity).days

            already_flagged = conn.execute(
                "SELECT 1 FROM purge_flags WHERE user_id = ?", (member.id,)
            ).fetchone()

            if days_silent >= threshold_days and not already_flagged:
                conn.execute(
                    "INSERT INTO purge_flags (user_id, flagged_at, reason) VALUES (?, ?, ?)",
                    (member.id, now.isoformat(), f"{days_silent} days silent")
                )
                conn.commit()

                hiatus_note = " (was on hiatus — got the extended window)" if on_hiatus else ""
                if alert_channel:
                    await alert_channel.send(
                        f"🕸️ **{member.display_name}** has been silent for **{days_silent} days**"
                        f"{hiatus_note} — past the inactivity threshold. Might be time to review/remove them."
                    )
                else:
                    print(f"⚠️ Alert channel '{STRIKE_ALERT_CHANNEL}' not found — couldn't post purge flag for {member.display_name}")

    conn.close()


async def purge_check_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await run_purge_check()
        await asyncio.sleep(PURGE_CHECK_INTERVAL_HOURS * 3600)


def compute_member_activity(user_id: int):
    """Returns (tier, avg_daily_messages, last_seen_iso, last_channel) for one user."""
    conn = get_db()
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=30)).isoformat()

    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM message_log WHERE user_id = ? AND posted_at >= ?",
        (user_id, window_start)
    ).fetchone()
    avg_daily = round((row["cnt"] or 0) / 30, 1)

    last_row = conn.execute(
        "SELECT channel_name, posted_at FROM message_log WHERE user_id = ? ORDER BY posted_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()

    if last_row is None:
        return ("Rarely Posts", 0.0, None, None)

    last_seen = datetime.fromisoformat(last_row["posted_at"])
    days_since = (now - last_seen).days

    high_threshold, medium_threshold, _ = get_current_thresholds()

    if days_since >= 30:
        tier = "Rarely Posts"
    elif days_since >= 15:
        tier = "Occasional"
    elif avg_daily >= high_threshold:
        tier = "Really Active"
    elif avg_daily >= medium_threshold:
        tier = "Active"
    else:
        tier = "Occasional"

    return (tier, avg_daily, last_row["posted_at"], last_row["channel_name"])


async def sync_activity_once():
    if not ACTIVITY_SYNC_SECRET:
        print("⚠️ ACTIVITY_SYNC_SECRET not set — skipping activity sync")
        return

    for guild in bot.guilds:
        if not guild.chunked:
            await guild.chunk()

        updates = []
        for member in guild.members:
            if member.bot:
                continue
            tier, avg_daily, last_seen, last_channel = compute_member_activity(member.id)
            updates.append({
                "discord_id": str(member.id),
                "display_name": member.display_name,
                "tier": tier,
                "avg_daily_messages": avg_daily,
                "last_seen_at": last_seen,
                "last_channel": last_channel,
            })

        if not updates:
            continue

        high_threshold, medium_threshold, computed_at = get_current_thresholds()

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    ACTIVITY_SYNC_URL,
                    json={
                        "updates": updates,
                        "thresholds": {
                            "high_threshold": high_threshold,
                            "medium_threshold": medium_threshold,
                            "computed_at": computed_at,
                        },
                    },
                    headers={"X-Sync-Secret": ACTIVITY_SYNC_SECRET},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    result = await resp.json()
                    print(f"📊 Activity sync: {result.get('updated', 0)} members updated")
            except Exception as e:
                print(f"⚠️ Activity sync failed: {e}")


async def activity_sync_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await sync_activity_once()
        await asyncio.sleep(ACTIVITY_SYNC_INTERVAL_HOURS * 3600)


async def prune_message_log_loop():
    """Keeps message_log from growing forever — deletes anything older
    than the retention window once a day."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        cutoff = (datetime.now(timezone.utc) - timedelta(days=MESSAGE_LOG_RETENTION_DAYS)).isoformat()
        conn = get_db()
        conn.execute("DELETE FROM message_log WHERE posted_at < ?", (cutoff,))
        conn.commit()
        conn.close()
        await asyncio.sleep(24 * 3600)


# ============================================================
#  STRIKE SYSTEM — mod-only, Discord-side only (no website
#  component, per Sunny's direction). Temporary strikes fade
#  automatically; permanent ones don't.
# ============================================================

def get_active_strikes(user_id: int):
    conn = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = conn.execute("""
        SELECT * FROM strikes
        WHERE user_id = ?
        AND (strike_type = 'permanent' OR (strike_type = 'temporary' AND expires_at > ?))
        ORDER BY issued_at DESC
    """, (user_id, now_iso)).fetchall()
    conn.close()
    return rows


@bot.tree.command(name="strike", description="[Mod] Issue a strike to a member")
@app_commands.describe(
    member="Who this strike is for",
    strike_type="Temporary strikes fade automatically; permanent ones don't",
    reason="What happened",
)
@app_commands.choices(strike_type=[
    app_commands.Choice(name="Temporary (fades in ~45 days)", value="temporary"),
    app_commands.Choice(name="Permanent", value="permanent"),
])
async def strike(interaction: discord.Interaction, member: discord.Member, strike_type: app_commands.Choice[str], reason: str):
    if not any(r.name in BOT_MOD_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=TEMP_STRIKE_DURATION_DAYS)).isoformat() if strike_type.value == "temporary" else None

    conn = get_db()
    conn.execute(
        "INSERT INTO strikes (user_id, strike_type, reason, issued_by, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (member.id, strike_type.value, reason, str(interaction.user), now.isoformat(), expires_at)
    )
    conn.commit()
    conn.close()

    active = get_active_strikes(member.id)

    await interaction.response.send_message(
        f"✅ {strike_type.name} strike issued to {member.display_name}. They now have **{len(active)}** active strike(s).",
        ephemeral=True
    )

    if len(active) >= STRIKE_ALERT_THRESHOLD:
        alert_channel = discord.utils.get(interaction.guild.text_channels, name=STRIKE_ALERT_CHANNEL)
        if alert_channel:
            await alert_channel.send(
                f"⚠️ **{member.display_name}** has reached **{len(active)}** active strikes. "
                f"Might be time for a serious conversation with them."
            )
        else:
            print(f"⚠️ Strike alert channel '{STRIKE_ALERT_CHANNEL}' not found — couldn't post alert")


@bot.tree.command(name="strikes", description="[Mod] View a member's active strike history")
@app_commands.describe(member="Whose strikes to look up")
async def strikes(interaction: discord.Interaction, member: discord.Member):
    if not any(r.name in BOT_MOD_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return

    active = get_active_strikes(member.id)

    if not active:
        await interaction.response.send_message(f"{member.display_name} has no active strikes.", ephemeral=True)
        return

    lines = [f"**Active strikes for {member.display_name}:**"]
    for s in active:
        expiry_note = f" (fades {s['expires_at'][:10]})" if s["strike_type"] == "temporary" else " (permanent)"
        lines.append(f"`#{s['id']}` — {s['reason']} — issued by {s['issued_by']} on {s['issued_at'][:10]}{expiry_note}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="removestrike", description="[Mod] Remove a strike by its ID (see /strikes for IDs)")
@app_commands.describe(strike_id="The strike ID shown in /strikes")
async def removestrike(interaction: discord.Interaction, strike_id: int):
    if not any(r.name in BOT_MOD_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return

    conn = get_db()
    cursor = conn.execute("DELETE FROM strikes WHERE id = ?", (strike_id,))
    conn.commit()
    conn.close()

    if cursor.rowcount > 0:
        await interaction.response.send_message(f"✅ Strike `#{strike_id}` removed.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ No strike found with ID `#{strike_id}`.", ephemeral=True)


# ============================================================
#  MINOR VIOLATION ESCALATION LADDER
#  Level 1 (30 day CD) -> retrigger inside window -> Level 2 (90 day CD)
#  -> retrigger inside window -> flagged for manual ban (never auto-banned).
#  A fully-expired cooldown with no retrigger resets back to Level 1.
# ============================================================

def get_next_violation_level(user_id: int) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT level, issued_at FROM minor_violation_log WHERE user_id = ? ORDER BY issued_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()

    if row is None:
        return 1

    # Already flagged for a ban that hasn't happened yet — keep re-flagging,
    # there's no level beyond this.
    if row["level"] >= 3:
        return 3

    cooldown_days = MINOR_VIOLATION_LEVEL1_COOLDOWN_DAYS if row["level"] == 1 else MINOR_VIOLATION_LEVEL2_COOLDOWN_DAYS
    issued_at = datetime.fromisoformat(row["issued_at"])
    cooldown_expired = datetime.now(timezone.utc) > issued_at + timedelta(days=cooldown_days)

    if cooldown_expired:
        return 1  # clean slate
    return row["level"] + 1  # escalate


@bot.tree.command(name="minorviolation", description="[Mod] Log a minor rule violation (escalates automatically on repeat offenses)")
@app_commands.describe(member="Who this is for", reason="What happened")
async def minorviolation(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not any(r.name in BOT_MOD_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return

    new_level = get_next_violation_level(member.id)
    now = datetime.now(timezone.utc)

    conn = get_db()
    conn.execute(
        "INSERT INTO minor_violation_log (user_id, level, reason, issued_by, issued_at) VALUES (?, ?, ?, ?, ?)",
        (member.id, new_level, reason, str(interaction.user), now.isoformat())
    )
    conn.commit()
    conn.close()

    if new_level == 1:
        await interaction.response.send_message(
            f"✅ Logged as a **Level 1** minor violation for {member.display_name} (30-day cooldown started).",
            ephemeral=True
        )
    elif new_level == 2:
        await interaction.response.send_message(
            f"⚠️ {member.display_name} re-offended within the cooldown window — escalated to **Level 2** (90-day cooldown). One more within that window and they'll be flagged for a ban review.",
            ephemeral=True
        )
        alert_channel = discord.utils.get(interaction.guild.text_channels, name=STRIKE_ALERT_CHANNEL)
        if alert_channel:
            await alert_channel.send(
                f"⚠️ **{member.display_name}** has escalated to **Level 2** minor violations "
                f"(re-offended within the 30-day window). Latest reason: {reason}"
            )
    else:  # new_level == 3
        await interaction.response.send_message(
            f"🚨 {member.display_name} has triggered a **3rd violation** within the escalation window. "
            f"This is a ban recommendation, not an automatic ban — see the mod-alerts channel.",
            ephemeral=True
        )
        alert_channel = discord.utils.get(interaction.guild.text_channels, name=STRIKE_ALERT_CHANNEL)
        if alert_channel:
            await alert_channel.send(
                f"🚨 **BAN REVIEW NEEDED:** {member.display_name} has hit a 3rd minor violation "
                f"within the escalation window (Level 2 -> retrigger). Per guild policy this calls for a ban. "
                f"Latest reason: {reason}\n"
                f"The bot will NOT ban automatically — a mod needs to review and take action manually."
            )
        else:
            print(f"⚠️ Alert channel '{STRIKE_ALERT_CHANNEL}' not found — couldn't post ban-review flag")


@bot.tree.command(name="violations", description="[Mod] View a member's minor violation history and current status")
@app_commands.describe(member="Whose violation history to look up")
async def violations(interaction: discord.Interaction, member: discord.Member):
    if not any(r.name in BOT_MOD_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM minor_violation_log WHERE user_id = ? ORDER BY issued_at DESC LIMIT 10",
        (member.id,)
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message(f"{member.display_name} has no minor violation history.", ephemeral=True)
        return

    current_level = get_next_violation_level(member.id)
    status_note = {
        1: "Clean slate — no active cooldown (or none yet logged).",
        2: "Currently at Level 1, inside its 30-day cooldown.",
        3: "Currently at Level 2 or pending ban review — inside its cooldown, or flagged.",
    }[current_level] if rows else "No history."

    lines = [f"**Minor violation history for {member.display_name}:**", f"_{status_note}_", ""]
    for r in rows:
        lines.append(f"Level {r['level']} — {r['reason']} — by {r['issued_by']} on {r['issued_at'][:10]}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="clearpurgeflag", description="[Mod] Dismiss an inactivity purge flag for a member")
@app_commands.describe(member="Who to clear the flag for")
async def clearpurgeflag(interaction: discord.Interaction, member: discord.Member):
    if not any(r.name in BOT_MOD_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return

    conn = get_db()
    cursor = conn.execute("DELETE FROM purge_flags WHERE user_id = ?", (member.id,))
    conn.commit()
    conn.close()

    if cursor.rowcount > 0:
        await interaction.response.send_message(f"✅ Purge flag cleared for {member.display_name}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"{member.display_name} wasn't flagged.", ephemeral=True)


@bot.tree.command(name="foodclubreset", description="[Mod] Clear today's Food Club check and re-run it immediately")
async def foodclubreset(interaction: discord.Interaction):
    if not any(r.name in BOT_MOD_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db_delete_food_club_status(today_str)
    print(f"🥕 {interaction.user.display_name} manually reset Food Club status for {today_str}")

    result = await run_food_club_check()
    await interaction.followup.send(f"✅ Re-ran the check.\n**Result:** {result}", ephemeral=True)


# ============================================================
#  RUN
# ============================================================

init_db()
bot.run(BOT_TOKEN)
