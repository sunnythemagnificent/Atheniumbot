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
FOOD_CLUB_PING_ROLE = "Food Club Betters"  # Role to ping on High/Very High outlook days
FOOD_CLUB_REDDIT_USER = "nsheng"           # Reddit user who posts the daily bets
FOOD_CLUB_CHECK_INTERVAL_HOURS = 3         # How often to re-check if today's post isn't up yet

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS food_club_status (
            date TEXT PRIMARY KEY,
            outlook TEXT,
            pinged INTEGER
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
    Parse item lines into a list of (name, avg_value, is_priority, display) tuples.
    Accepts two formats:
      - 'Item Name:Value'        e.g. 'Liquid Glass Filter:4'
      - 'Item Name - Value Caps' e.g. 'Liquid Glass Filter - 4 Caps'
    Both formats support:
      - Ranges, e.g. 'Item:1-2' or 'Item - 1-2 Caps' — the midpoint is used for math,
        the original range is kept for display.
      - A '*' prefix on the name to mark it as a must-keep priority item.
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
        avg_value = (low + high) / 2
        display = f"{low:g}" if low == high else f"{low:g}-{high:g}"

        items.append((name, avg_value, is_priority, display))

    return items, errors


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

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class FairTradeStartView(discord.ui.View):
    """Shown before the modal so we have room for full instructions (modal labels cap at 45 characters)."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Open Trade Form", style=discord.ButtonStyle.primary, emoji="📝")
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FairTradeModal())


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
        "**Priority items:** put a `*` before the name to mark it as a must-keep —\n"
        "`*Rare Item:5` — it will never be suggested for removal.\n\n"
        "Click below when you're ready to fill out your items!"
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
    bot.loop.create_task(food_club_check_loop())


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
#  FOOD CLUB OUTLOOK CHECKER
# ============================================================

ATOM_NS = "{http://www.w3.org/2005/Atom}"

REDDIT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def parse_food_club_bets(content_html):
    """Extract NFC bet links for each betting level from a comment's rendered HTML content."""
    pattern = re.compile(
        r'(Beginner|Standard|Aggressive|Adventurous):\s*<a[^>]+href="(https://neofood\.club/[^"]+)"',
        re.IGNORECASE
    )
    bets = {}
    for m in pattern.finditer(content_html):
        level = m.group(1).capitalize()
        bets[level] = html.unescape(m.group(2))
    return bets


async def fetch_rss(url):
    """Fetch and parse an Atom/RSS feed. Returns the parsed XML root, or None on failure."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=REDDIT_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"⚠️ Food Club: RSS returned status {resp.status} for {url}")
                    return None
                text = await resp.text()
    except Exception as e:
        print(f"⚠️ Food Club: couldn't reach {url} — {e}")
        return None

    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        print(f"⚠️ Food Club: couldn't parse RSS feed — {e}")
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
    comment inside it containing the outlook and NFC bet links.
    Returns (outlook_text, comment_url, bets_dict) if found, or (None, None, None)
    if the thread or comment isn't up yet, or something went wrong.
    """
    thread_url = await find_todays_food_club_thread()
    if not thread_url:
        return None, None, None

    comments_rss_url = thread_url if thread_url.endswith("/") else thread_url + "/"
    comments_rss_url += ".rss"

    root = await fetch_rss(comments_rss_url)
    if root is None:
        return None, None, None

    for entry in root.findall(f"{ATOM_NS}entry"):
        author_el = entry.find(f"{ATOM_NS}author/{ATOM_NS}name")
        author = (author_el.text or "") if author_el is not None else ""

        if FOOD_CLUB_REDDIT_USER.lower() not in author.lower():
            continue

        content_el = entry.find(f"{ATOM_NS}content")
        content_html = (content_el.text or "") if content_el is not None else ""

        match = re.search(r"outlook for this round:\s*([^<\n]+)", content_html, re.IGNORECASE)
        if not match:
            continue

        outlook_text = html.unescape(match.group(1).strip())
        bets = parse_food_club_bets(content_html)

        link_el = entry.find(f"{ATOM_NS}link")
        comment_url = link_el.get("href") if link_el is not None else thread_url

        return outlook_text, comment_url, bets

    print(f"🥕 Food Club: found today's thread but no matching comment from u/{FOOD_CLUB_REDDIT_USER} yet")
    return None, None, None


async def food_club_check_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = db_get_food_club_status(today_str)

        if existing is None:
            outlook_text, post_url, bets = await fetch_food_club_outlook()

            if outlook_text:
                is_high = "high" in outlook_text.lower()
                pinged = False

                if is_high:
                    for guild in bot.guilds:
                        channel = discord.utils.get(guild.text_channels, name=FOOD_CLUB_CHANNEL)
                        role = discord.utils.get(guild.roles, name=FOOD_CLUB_PING_ROLE)
                        if channel and role:
                            try:
                                message_lines = [f"{role.mention} 🥕 Today's Food Club outlook: **{outlook_text}**!"]

                                if bets:
                                    message_lines.append("")
                                    level_order = ["Beginner", "Standard", "Aggressive", "Adventurous"]
                                    for level in level_order:
                                        if level in bets:
                                            message_lines.append(f"**{level}:** [NFC link]({bets[level]})")

                                if post_url:
                                    message_lines.append("")
                                    message_lines.append(post_url)

                                await channel.send("\n".join(message_lines))
                                pinged = True
                                print(f"🥕 Pinged {FOOD_CLUB_PING_ROLE} — outlook: {outlook_text}")
                            except Exception as e:
                                print(f"⚠️ Food Club: couldn't send ping — {e}")

                db_set_food_club_status(today_str, outlook_text, pinged)
                if not is_high:
                    print(f"🥕 Food Club outlook today: '{outlook_text}' — no ping needed")
            else:
                print(f"🥕 Food Club: no post found for {today_str} yet, will check again in {FOOD_CLUB_CHECK_INTERVAL_HOURS}h")

        await asyncio.sleep(FOOD_CLUB_CHECK_INTERVAL_HOURS * 3600)


# ============================================================
#  RUN
# ============================================================

init_db()
bot.run(BOT_TOKEN)
