"""
points.py - Event points + prize shop for AtheniumBot
=====================================================

Everything for the points system lives in this one file so main.py only needs
a handful of one-line hooks (see the bottom of this docstring).

HOW IT WORKS
- Mods run /event start to create an EVENT. Each event has three phases:
    1. EARNING  - members earn points automatically from chatting, and mods can
                  hand out bonus points with /awardpoints. The shop is open too.
    2. SHOP     - for a few days after earning ends, nobody earns anything new
                  but members can still spend what they have.
    3. CLOSED   - unspent points expire. Nothing is deleted; the full history
                  stays on record, the points just can't be spent anymore.
- Points belong to ONE event. A member can be spending last event's points while
  the next event is already earning. Only one event can be in its earning phase
  at a time.
- Mods stock each event's shop with /shopmod. Members use /shop and /redeem.
  A redemption posts a request in the mod channel with Fulfill / Deny buttons.
  Prizes in Neopets are delivered by hand by a mod. Deny refunds automatically.
- A prize can optionally be a Discord role, which is granted instantly.
- Each event can limit where points are earned to specific channels or categories
  (picked from a dropdown right after /event start, or later with /event channels).
  With nothing picked, points are earned in every channel except the usual exclusions.

HOOKS NEEDED IN main.py (4 small edits):
    import points                                           (near the top)
    points.setup(bot, get_db, ...)                          (just before bot.run)
    await points.on_message_points(message)                (in on_message)
    points.start_background(bot)                            (in on_ready)
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

# ============================================================
#  CONFIGURATION - edit these values
# ============================================================

# The main server's name is added automatically. Add the exact name of any test
# server here if you want to try the points system there too.
POINTS_GUILD_NAMES_EXTRA = ["Bot Testing Playground"]

# Channel NAME where event announcements are posted (no # symbol). If None, the
# announcement goes in whichever channel the mod ran /event start in (or the
# channel they pick in the command).
POINTS_ANNOUNCE_CHANNEL = None

# Automatic points rules (the points-per-message and daily cap can also be set
# per event when you run /event start; these are the defaults and global rules)
POINTS_MESSAGE_COOLDOWN_SECONDS = 60   # at most one scoring message per member per minute
POINTS_MIN_MESSAGE_CHARS = 10          # shorter messages (like "lol") don't count
DEFAULT_POINTS_PER_MESSAGE = 1
DEFAULT_DAILY_CAP = 10                 # max automatic points per member per day (Pacific day)
DEFAULT_SHOP_DAYS = 3                  # days the shop stays open after earning ends

POINTS_PUBLIC_AWARDS = True            # True = /awardpoints is announced in the channel it's used in
POINTS_ALLOW_SELF_AWARD = False        # False = mods can't award points to themselves
MAX_AWARD = 10000                      # biggest single /awardpoints or /removepoints

POINTS_LOOP_SECONDS = 60               # how often the bot checks event phases
CLOSE_REMINDER_HOURS = 24              # "last call" reminder this long before the shop closes

EMBED_COLOR = 0xD68A4E
PACIFIC = ZoneInfo("America/Los_Angeles")

# ============================================================
#  STATE (filled in by setup())
# ============================================================

_bot = None
_get_db_fn = None
_mod_roles = []
_guild_names = []
_excluded_channels = set()
_request_channel_name = ""

_last_scored = {}      # (event_id, user_id) -> datetime of last scoring message
_capped_until = {}     # (event_id, user_id) -> datetime when today's cap resets
_cache = {"until": None, "event": None}


def _now():
    return datetime.now(timezone.utc)


# ============================================================
#  TIME HELPERS
#  All timestamps are stored as fixed-format UTC strings so that comparing
#  them as text is always the same as comparing them as times.
# ============================================================

_FMT = "%Y-%m-%d %H:%M:%S"


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime(_FMT)


def _parse(s):
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


def _ts(s, style="f"):
    """A Discord timestamp tag - shows in each viewer's own local time."""
    return f"<t:{int(_parse(s).timestamp())}:{style}>"


def _pacific_day_start(now):
    local = now.astimezone(PACIFIC)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


# ============================================================
#  DATABASE
# ============================================================

@contextmanager
def _db():
    conn = _get_db_fn()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _txn():
    """A write transaction that locks the database up front, so two people
    spending points at the same moment can never both succeed on the same balance."""
    conn = _get_db_fn()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def init_tables():
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pt_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                earn_start TEXT NOT NULL,
                earn_end TEXT NOT NULL,
                shop_close TEXT NOT NULL,
                shop_days INTEGER NOT NULL,
                auto_points INTEGER NOT NULL DEFAULT 1,
                points_per_message INTEGER NOT NULL,
                daily_cap INTEGER NOT NULL,
                announce_channel_id INTEGER,
                announced_open INTEGER NOT NULL DEFAULT 0,
                announced_end INTEGER NOT NULL DEFAULT 0,
                reminded_close INTEGER NOT NULL DEFAULT 0,
                announced_close INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pt_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                kind TEXT NOT NULL,
                reason TEXT,
                actor_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                ref_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_pt_ledger_user ON pt_ledger(event_id, user_id);
            CREATE TABLE IF NOT EXISTS pt_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                cost INTEGER NOT NULL,
                stock INTEGER,
                per_user_limit INTEGER,
                role_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pt_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                cost INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by INTEGER,
                note TEXT,
                request_guild_id INTEGER,
                request_channel_id INTEGER,
                request_message_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_pt_red_msg ON pt_redemptions(request_message_id);
            CREATE TABLE IF NOT EXISTS pt_event_channels (
                event_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                PRIMARY KEY (event_id, channel_id)
            );
        """)
        # Upgrade path for databases created before channel lists existed
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(pt_events)").fetchall()}
        if "announce_message_id" not in cols:
            conn.execute("ALTER TABLE pt_events ADD COLUMN announce_message_id INTEGER")
        conn.commit()


def _add_ledger(conn, event_id, user_id, amount, kind, reason, actor_id, now, ref_id=None):
    conn.execute(
        "INSERT INTO pt_ledger (event_id, user_id, amount, kind, reason, actor_id, created_at, ref_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, user_id, amount, kind, reason, actor_id, _iso(now), ref_id)
    )


def _balance(conn, event_id, user_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS b FROM pt_ledger WHERE event_id = ? AND user_id = ?",
        (event_id, user_id)
    ).fetchone()
    return row["b"]


def get_balance(event_id, user_id):
    with _db() as conn:
        return _balance(conn, event_id, user_id)


# ============================================================
#  EVENTS
# ============================================================

PHASE_LABELS = {
    "scheduled": "⏳ Upcoming",
    "earning": "🟢 Earning + shop open",
    "shop": "🛍️ Shop only",
    "closed": "🔒 Closed",
    "cancelled": "🚫 Cancelled",
}


def event_phase(ev, now=None):
    now = now or _now()
    if ev["cancelled"]:
        return "cancelled"
    if now < _parse(ev["earn_start"]):
        return "scheduled"
    if now < _parse(ev["earn_end"]):
        return "earning"
    if now < _parse(ev["shop_close"]):
        return "shop"
    return "closed"


def get_event(event_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM pt_events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def list_events(limit=50):
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM pt_events WHERE cancelled = 0 ORDER BY earn_start DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def open_events(now=None):
    """Events whose shop is currently open (earning or shop-only), oldest first."""
    now = now or _now()
    return [e for e in reversed(list_events()) if event_phase(e, now) in ("earning", "shop")]


def invalidate_cache():
    _cache["until"] = None


# ---------- where an event lets people earn points ----------

def get_event_channels(event_id):
    """Returns (channel_ids, category_ids) as two sets. Both empty = no restriction."""
    with _db() as conn:
        rows = conn.execute("SELECT channel_id, kind FROM pt_event_channels WHERE event_id = ?", (event_id,)).fetchall()
    chans = {r["channel_id"] for r in rows if r["kind"] == "channel"}
    cats = {r["channel_id"] for r in rows if r["kind"] == "category"}
    return chans, cats


def set_event_channels(event_id, picks):
    """Replaces the event's list. picks = [(channel_id, 'channel' | 'category'), ...].
    An empty list removes the restriction (points can be earned in all the usual channels)."""
    with _txn() as conn:
        conn.execute("DELETE FROM pt_event_channels WHERE event_id = ?", (event_id,))
        for channel_id, kind in picks:
            conn.execute(
                "INSERT OR REPLACE INTO pt_event_channels (event_id, channel_id, kind) VALUES (?, ?, ?)",
                (event_id, channel_id, kind)
            )
    invalidate_cache()


def _channel_allowed(channel, chans, cats):
    """Is this channel one the event allows? Posts inside threads and forum posts
    count as their parent channel, and picking a category allows everything in it."""
    ids = {getattr(channel, "id", None), getattr(channel, "parent_id", None)} - {None}
    if ids & chans:
        return True
    category_id = getattr(channel, "category_id", None)
    if category_id is None:
        category_id = getattr(getattr(channel, "parent", None), "category_id", None)
    return category_id is not None and category_id in cats


def channels_text(event_id):
    """Plain-language description of where points can be earned."""
    chans, cats = get_event_channels(event_id)
    if not chans and not cats:
        return "Most channels in the server (not the mod, giveaway, or other ignored channels)"
    parts = [f"<#{c}>" for c in sorted(chans)] + [f"everything in <#{c}> (category)" for c in sorted(cats)]
    text = ", ".join(parts)
    return text if len(text) <= 900 else text[:900] + "…"


def _current_earning_event(now):
    """The one event (if any) currently in its earning phase. Cached briefly so
    that every chat message doesn't cost a database lookup."""
    if _cache["until"] is not None and now < _cache["until"]:
        ev = _cache["event"]
        if ev is None or event_phase(ev, now) == "earning":
            return ev
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM pt_events WHERE cancelled = 0 AND earn_start <= ? AND earn_end > ? LIMIT 1",
            (_iso(now), _iso(now))
        ).fetchone()
    ev = dict(row) if row else None
    if ev:
        chans, cats = get_event_channels(ev["id"])
        ev["_rules"] = (chans, cats) if (chans or cats) else None
    _cache["event"] = ev
    _cache["until"] = now + timedelta(seconds=15)
    return ev


def _target_event_for_mod(now):
    """The event a mod's /awardpoints applies to: the one currently earning."""
    return _current_earning_event(now)


def _event_for_removal(now):
    """/removepoints works in the earning phase, or the shop phase if nothing is earning."""
    ev = _current_earning_event(now)
    if ev:
        return ev
    shops = [e for e in open_events(now) if event_phase(e, now) == "shop"]
    return shops[-1] if shops else None


def create_event(name, created_by, earn_days, shop_days, start_dt, auto_points,
                 points_per_message, daily_cap, announce_channel_id, now):
    """Returns (True, event_id, start, end, close) or (False, clashing_event_name, None, None, None)."""
    start = max(start_dt, now)
    end = start + timedelta(days=earn_days)
    close = end + timedelta(days=shop_days)
    with _txn() as conn:
        clash = conn.execute(
            "SELECT name FROM pt_events WHERE cancelled = 0 AND earn_start < ? AND ? < earn_end",
            (_iso(end), _iso(start))
        ).fetchone()
        if clash:
            return (False, clash["name"], None, None, None)
        cur = conn.execute(
            "INSERT INTO pt_events (name, created_by, created_at, earn_start, earn_end, shop_close, shop_days, "
            "auto_points, points_per_message, daily_cap, announce_channel_id, reminded_close) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, created_by, _iso(now), _iso(start), _iso(end), _iso(close), shop_days,
             1 if auto_points else 0, points_per_message, daily_cap, announce_channel_id,
             1 if shop_days <= 1 else 0)
        )
        event_id = cur.lastrowid
    invalidate_cache()
    return (True, event_id, start, end, close)


def end_event_now(event_id, now):
    """Stop earning right now; the shop then stays open for the event's shop_days."""
    with _txn() as conn:
        row = conn.execute("SELECT * FROM pt_events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            return (False, "missing")
        ev = dict(row)
        phase = event_phase(ev, now)
        if phase != "earning":
            return (False, phase)
        close = now + timedelta(days=ev["shop_days"])
        conn.execute(
            "UPDATE pt_events SET earn_end = ?, shop_close = ?, announced_open = 1, announced_end = 0, "
            "reminded_close = ? WHERE id = ?",
            (_iso(now), _iso(close), 1 if ev["shop_days"] <= 1 else 0, event_id)
        )
    invalidate_cache()
    return (True, "ok")


def close_event_now(event_id, now):
    """Close the shop right now (and stop earning if it hasn't already)."""
    with _txn() as conn:
        row = conn.execute("SELECT * FROM pt_events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            return (False, "missing")
        ev = dict(row)
        phase = event_phase(ev, now)
        if phase not in ("earning", "shop"):
            return (False, phase)
        new_end = _iso(now) if phase == "earning" else ev["earn_end"]
        conn.execute(
            "UPDATE pt_events SET earn_end = ?, shop_close = ?, announced_open = 1, announced_end = 1, "
            "reminded_close = 1, announced_close = 0 WHERE id = ?",
            (new_end, _iso(now), event_id)
        )
    invalidate_cache()
    return (True, "ok")


def cancel_event(event_id, now):
    with _txn() as conn:
        row = conn.execute("SELECT * FROM pt_events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            return (False, "missing")
        phase = event_phase(dict(row), now)
        if phase != "scheduled":
            return (False, phase)
        conn.execute("UPDATE pt_events SET cancelled = 1 WHERE id = ?", (event_id,))
    invalidate_cache()
    return (True, "ok")


# ============================================================
#  AUTOMATIC POINTS FROM CHATTING
# ============================================================

async def on_message_points(message):
    """Called from main.py's on_message. Never raises, so a problem here can't
    break message logging or the Active role."""
    try:
        _score_message(message)
    except Exception as e:
        print(f"⚠️ Points: couldn't score a message: {e}")


def _score_message(message, now=None):
    if message.guild is None or message.author.bot:
        return
    if message.guild.name not in _guild_names:
        return
    if len((message.content or "").strip()) < POINTS_MIN_MESSAGE_CHARS:
        return

    now = now or _now()
    ev = _current_earning_event(now)
    if not ev or not ev["auto_points"]:
        return

    # Where can points be earned? If the mod picked channels for this event, ONLY
    # those count. If they didn't, every channel counts except the usual exclusions.
    rules = ev.get("_rules")
    if rules:
        if not _channel_allowed(message.channel, rules[0], rules[1]):
            return
    elif message.channel.name in _excluded_channels:
        return

    key = (ev["id"], message.author.id)
    capped_until = _capped_until.get(key)
    if capped_until and now < capped_until:
        return
    last = _last_scored.get(key)
    if last and (now - last).total_seconds() < POINTS_MESSAGE_COOLDOWN_SECONDS:
        return

    day_start = _pacific_day_start(now)
    with _db() as conn:
        earned_today = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS t FROM pt_ledger "
            "WHERE event_id = ? AND user_id = ? AND kind = 'auto' AND created_at >= ?",
            (ev["id"], message.author.id, _iso(day_start))
        ).fetchone()["t"]
        remaining = ev["daily_cap"] - earned_today
        if remaining <= 0:
            _capped_until[key] = day_start + timedelta(days=1)
            return
        grant = min(ev["points_per_message"], remaining)
        _add_ledger(conn, ev["id"], message.author.id, grant, "auto", "Chatting", 0, now)
        conn.commit()

    _last_scored[key] = now
    if grant >= remaining:
        _capped_until[key] = day_start + timedelta(days=1)


# ============================================================
#  SHOP + REDEMPTION LOGIC (database side)
# ============================================================

def get_item(item_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM pt_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def items_for_event(event_id, only_active=True):
    with _db() as conn:
        sql = "SELECT * FROM pt_items WHERE event_id = ?" + (" AND active = 1" if only_active else "") + " ORDER BY cost, id"
        rows = conn.execute(sql, (event_id,)).fetchall()
    return [dict(r) for r in rows]


def get_redemption(red_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM pt_redemptions WHERE id = ?", (red_id,)).fetchone()
    return dict(row) if row else None


def get_redemption_by_message(message_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM pt_redemptions WHERE request_message_id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def attempt_redeem(user_id, item_id, now):
    """Checks everything and spends the points in a single locked transaction.
    Returns (True, 'ok', data) or (False, reason_text, None)."""
    with _txn() as conn:
        it = conn.execute("SELECT * FROM pt_items WHERE id = ?", (item_id,)).fetchone()
        if not it or not it["active"]:
            return (False, "That prize isn't available.", None)
        evrow = conn.execute("SELECT * FROM pt_events WHERE id = ?", (it["event_id"],)).fetchone()
        if not evrow or event_phase(dict(evrow), now) not in ("earning", "shop"):
            return (False, "That shop isn't open right now.", None)
        if it["stock"] is not None and it["stock"] <= 0:
            return (False, "Sorry, that prize is sold out.", None)
        if it["per_user_limit"] is not None:
            claimed = conn.execute(
                "SELECT COUNT(*) AS n FROM pt_redemptions WHERE user_id = ? AND item_id = ? AND status != 'denied'",
                (user_id, item_id)
            ).fetchone()["n"]
            if claimed >= it["per_user_limit"]:
                return (False, f"You've already claimed the limit for this prize ({it['per_user_limit']} per member).", None)
        balance = _balance(conn, evrow["id"], user_id)
        if balance < it["cost"]:
            return (False, f"That costs {it['cost']:,} points, but you only have {balance:,}.", None)

        if it["stock"] is not None:
            conn.execute("UPDATE pt_items SET stock = stock - 1 WHERE id = ?", (item_id,))
        cur = conn.execute(
            "INSERT INTO pt_redemptions (event_id, item_id, item_name, cost, user_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (evrow["id"], it["id"], it["name"], it["cost"], user_id, _iso(now))
        )
        red_id = cur.lastrowid
        _add_ledger(conn, evrow["id"], user_id, -it["cost"], "spend", f"Redeemed: {it['name']}", 0, now, red_id)
        return (True, "ok", {
            "redemption_id": red_id,
            "item": dict(it),
            "event": dict(evrow),
            "balance": balance - it["cost"],
        })


def resolve_redemption(red_id, new_status, actor_id, note, now):
    """Marks a pending redemption fulfilled or denied. Denying refunds the points
    and restocks the prize. Returns (True, 'ok', row) or (False, reason, row)."""
    with _txn() as conn:
        row = conn.execute("SELECT * FROM pt_redemptions WHERE id = ?", (red_id,)).fetchone()
        if not row:
            return (False, "That request no longer exists.", None)
        if row["status"] != "pending":
            return (False, f"That request was already {row['status']}.", dict(row))
        if new_status == "denied":
            reason = f"Refund: {row['item_name']}" + (f" ({note})" if note else "")
            _add_ledger(conn, row["event_id"], row["user_id"], row["cost"], "refund", reason, actor_id, now, red_id)
            conn.execute("UPDATE pt_items SET stock = stock + 1 WHERE id = ? AND stock IS NOT NULL", (row["item_id"],))
        conn.execute(
            "UPDATE pt_redemptions SET status = ?, resolved_at = ?, resolved_by = ?, note = ? WHERE id = ?",
            (new_status, _iso(now), actor_id, note or None, red_id)
        )
        updated = conn.execute("SELECT * FROM pt_redemptions WHERE id = ?", (red_id,)).fetchone()
        return (True, "ok", dict(updated))


def _set_request_ref(red_id, guild_id, channel_id, message_id):
    with _db() as conn:
        conn.execute(
            "UPDATE pt_redemptions SET request_guild_id = ?, request_channel_id = ?, request_message_id = ? WHERE id = ?",
            (guild_id, channel_id, message_id, red_id)
        )
        conn.commit()


# ============================================================
#  SMALL DISCORD HELPERS
# ============================================================

def _is_mod(user):
    return any(r.name in _mod_roles for r in getattr(user, "roles", []))


def _find_request_channel(guild):
    return discord.utils.get(guild.text_channels, name=_request_channel_name)


async def _deny_wrong_guild(interaction):
    if interaction.guild is None or interaction.guild.name not in _guild_names:
        await interaction.response.send_message("⚠️ The points system isn't active in this server.", ephemeral=True)
        return True
    return False


async def _deny_non_mod(interaction):
    if not _is_mod(interaction.user):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return True
    return False


def _pts(n):
    return f"{n:,} point" + ("" if n == 1 else "s")


def _time_left_text(ev, phase):
    if phase == "earning":
        return f"Earning ends {_ts(ev['earn_end'], 'R')} · shop closes {_ts(ev['shop_close'], 'R')}"
    if phase == "shop":
        return f"No new points are being earned. Shop closes {_ts(ev['shop_close'], 'R')} — unspent points expire then."
    if phase == "scheduled":
        return f"Starts {_ts(ev['earn_start'], 'R')}"
    return ""


def _item_line(it):
    bits = [f"**#{it['id']} · {it['name']}** — {it['cost']:,} pts"]
    if it["stock"] is not None:
        bits.append("SOLD OUT" if it["stock"] <= 0 else f"{it['stock']} left")
    if it["per_user_limit"] is not None:
        bits.append(f"limit {it['per_user_limit']} each")
    if it["role_id"]:
        bits.append("🏷️ role reward (instant)")
    line = " · ".join(bits)
    if it["description"]:
        line += f"\n> {it['description']}"
    return line


async def _notify_user(user_id, text):
    try:
        user = _bot.get_user(user_id) or await _bot.fetch_user(user_id)
        await user.send(text)
    except Exception:
        pass  # DMs closed - nothing else to do


# ============================================================
#  ANNOUNCEMENTS + THE BACKGROUND LOOP
# ============================================================

async def _get_announce_channel(ev):
    cid = ev.get("announce_channel_id")
    if not cid:
        return None
    channel = _bot.get_channel(cid)
    if channel is None:
        try:
            channel = await _bot.fetch_channel(cid)
        except Exception:
            channel = None
    return channel


def _mark(event_id, column):
    assert column in ("announced_open", "announced_end", "reminded_close", "announced_close")
    with _db() as conn:
        conn.execute(f"UPDATE pt_events SET {column} = 1 WHERE id = ?", (event_id,))
        conn.commit()


async def _announce(ev, embed):
    """Posts an announcement and returns the sent message (or None if it couldn't)."""
    channel = await _get_announce_channel(ev)
    if channel is None:
        print(f"⚠️ Points: couldn't find the announcement channel for event '{ev['name']}'")
        return None
    try:
        return await channel.send(embed=embed)
    except Exception as e:
        print(f"⚠️ Points: couldn't post an announcement for '{ev['name']}': {e}")
        return None


def _set_announce_message(event_id, message_id):
    with _db() as conn:
        conn.execute("UPDATE pt_events SET announce_message_id = ? WHERE id = ?", (message_id, event_id))
        conn.commit()


async def _refresh_open_announcement(event_id):
    """If the opening announcement is already posted, rewrite it so it shows the
    event's current channels. Called whenever a mod changes them."""
    ev = get_event(event_id)
    if not ev or not ev.get("announce_message_id"):
        return
    channel = await _get_announce_channel(ev)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(ev["announce_message_id"])
        await message.edit(embed=_open_embed(ev))
    except Exception as e:
        print(f"⚠️ Points: couldn't update the opening announcement for '{ev['name']}': {e}")


def _open_embed(ev):
    if ev["auto_points"]:
        how = (f"Chat in the server to earn **{ev['points_per_message']}** point(s) per message "
               f"(up to **{ev['daily_cap']}** a day). Mods may also award bonus points.")
    else:
        how = "Mods will award points during this event — keep an eye out!"
    desc = (
        f"{how}\n\n"
        f"**Earning ends:** {_ts(ev['earn_end'])} ({_ts(ev['earn_end'], 'R')})\n"
        f"**Shop closes:** {_ts(ev['shop_close'])}\n\n"
        f"`/points` — check your balance\n`/shop` — browse the prizes\n`/redeem` — claim one"
    )
    embed = discord.Embed(title=f"🎉 {ev['name']} has begun!", description=desc, color=EMBED_COLOR)
    if ev["auto_points"]:
        embed.add_field(name="📍 Where you can earn points", value=channels_text(ev["id"]), inline=False)
    return embed


def _end_embed(ev):
    desc = (
        f"No more points can be earned, but the shop stays open until **{_ts(ev['shop_close'])}** "
        f"({_ts(ev['shop_close'], 'R')}).\n\n"
        f"Use `/points` to check your balance and `/redeem` to claim a prize. "
        f"**Unspent points expire when the shop closes.**"
    )
    return discord.Embed(title=f"⏰ Earning has ended for {ev['name']}", description=desc, color=EMBED_COLOR)


def _reminder_embed(ev):
    desc = (
        f"The **{ev['name']}** shop closes {_ts(ev['shop_close'], 'R')} ({_ts(ev['shop_close'])}).\n"
        f"Spend your points with `/redeem` before they expire!"
    )
    return discord.Embed(title="⌛ Last call for the prize shop", description=desc, color=EMBED_COLOR)


def _close_embed(ev):
    return discord.Embed(
        title=f"🔒 The {ev['name']} shop is now closed",
        description="Unspent points have expired. Thank you to everyone who took part!",
        color=EMBED_COLOR
    )


def _recap_embed(ev):
    with _db() as conn:
        def total(kind):
            return conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS t FROM pt_ledger WHERE event_id = ? AND kind = ?",
                (ev["id"], kind)
            ).fetchone()["t"]
        auto, awarded, removed, spent, refunded = total("auto"), total("award"), total("remove"), total("spend"), total("refund")
        people = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM pt_ledger WHERE event_id = ? AND kind IN ('auto', 'award')",
            (ev["id"],)
        ).fetchone()["n"]
        expired = conn.execute(
            "SELECT COALESCE(SUM(b), 0) AS t FROM "
            "(SELECT SUM(amount) AS b FROM pt_ledger WHERE event_id = ? GROUP BY user_id HAVING b > 0)",
            (ev["id"],)
        ).fetchone()["t"]
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM pt_redemptions WHERE event_id = ? GROUP BY status", (ev["id"],)
        ).fetchall()}
        pending = conn.execute(
            "SELECT * FROM pt_redemptions WHERE event_id = ? AND status = 'pending' ORDER BY id", (ev["id"],)
        ).fetchall()

    embed = discord.Embed(title=f"📊 Recap: {ev['name']}", color=EMBED_COLOR)
    embed.add_field(name="Members who earned points", value=str(people), inline=True)
    embed.add_field(name="Earned from chatting", value=f"{auto:,}", inline=True)
    embed.add_field(name="Awarded by mods", value=f"{awarded:,}" + (f" (−{abs(removed):,} removed)" if removed else ""), inline=True)
    embed.add_field(name="Spent in the shop", value=f"{abs(spent) - refunded:,}", inline=True)
    embed.add_field(name="Expired unspent", value=f"{expired:,}", inline=True)
    embed.add_field(
        name="Redemptions",
        value=(f"✅ {counts.get('fulfilled', 0)} fulfilled · ❌ {counts.get('denied', 0)} denied · "
               f"⏳ {counts.get('pending', 0)} still waiting"),
        inline=False
    )
    if pending:
        lines = []
        for r in pending[:15]:
            link = ""
            if r["request_guild_id"] and r["request_channel_id"] and r["request_message_id"]:
                link = f" — [jump](https://discord.com/channels/{r['request_guild_id']}/{r['request_channel_id']}/{r['request_message_id']})"
            lines.append(f"`#{r['id']}` <@{r['user_id']}> — {r['item_name']}{link}")
        more = f"\n…and {len(pending) - 15} more (see /redemptions)" if len(pending) > 15 else ""
        embed.add_field(name="⚠️ Still need a mod", value="\n".join(lines) + more, inline=False)
    embed.set_footer(text=f"Event #{ev['id']}")
    return embed


async def _post_mod_recap(ev):
    embed = _recap_embed(ev)
    for guild in _bot.guilds:
        if guild.name not in _guild_names:
            continue
        channel = _find_request_channel(guild)
        if channel:
            try:
                await channel.send(embed=embed)
            except Exception as e:
                print(f"⚠️ Points: couldn't post the recap for '{ev['name']}': {e}")


async def _process_event(ev, now):
    start, end, close = _parse(ev["earn_start"]), _parse(ev["earn_end"]), _parse(ev["shop_close"])

    if not ev["announced_open"] and now >= start:
        if now < end:                                  # don't announce a start that's already over
            sent = await _announce(ev, _open_embed(ev))
            if sent is not None:
                _set_announce_message(ev["id"], sent.id)
        _mark(ev["id"], "announced_open")
        ev["announced_open"] = 1

    if not ev["announced_end"] and now >= end:
        if now < close:                                # don't announce an end once the shop is closed too
            await _announce(ev, _end_embed(ev))
        _mark(ev["id"], "announced_end")
        ev["announced_end"] = 1

    if not ev["reminded_close"] and now >= close - timedelta(hours=CLOSE_REMINDER_HOURS):
        if now < close:
            await _announce(ev, _reminder_embed(ev))
        _mark(ev["id"], "reminded_close")
        ev["reminded_close"] = 1

    if not ev["announced_close"] and now >= close:
        await _announce(ev, _close_embed(ev))
        await _post_mod_recap(ev)
        _mark(ev["id"], "announced_close")
        ev["announced_close"] = 1


async def run_tick(now=None):
    """One pass over every event: posts any announcements that are due."""
    now = now or _now()
    with _db() as conn:
        events = [dict(r) for r in conn.execute("SELECT * FROM pt_events WHERE cancelled = 0").fetchall()]
    for ev in events:
        try:
            await _process_event(ev, now)
        except Exception as e:
            print(f"⚠️ Points: problem processing event #{ev['id']}: {e}")


async def points_loop():
    await _bot.wait_until_ready()
    while not _bot.is_closed():
        try:
            await run_tick()
        except Exception as e:
            print(f"⚠️ Points: loop error: {e}")
        await asyncio.sleep(POINTS_LOOP_SECONDS)


# ============================================================
#  REDEMPTION REQUESTS (the Fulfill / Deny message mods see)
# ============================================================

def _request_embed(red, event_name, status, resolver=None, note=None):
    colors = {"pending": 0xE6B422, "fulfilled": 0x4CAF50, "denied": 0xC0392B}
    titles = {
        "pending": f"🎟️ Redemption request #{red['id']}",
        "fulfilled": f"✅ Fulfilled — request #{red['id']}",
        "denied": f"❌ Denied & refunded — request #{red['id']}",
    }
    embed = discord.Embed(title=titles[status], color=colors[status])
    embed.add_field(name="Member", value=f"<@{red['user_id']}>", inline=True)
    embed.add_field(name="Prize", value=red["item_name"], inline=True)
    embed.add_field(name="Cost", value=f"{red['cost']:,} pts", inline=True)
    embed.add_field(name="Event", value=event_name, inline=True)
    embed.add_field(name="Requested", value=_ts(red["created_at"], "R"), inline=True)
    if status == "pending":
        embed.add_field(
            name="What to do",
            value="Send the prize in Neopets, then click **Fulfill**. Click **Deny + refund** if it can't be done — the points go back automatically.",
            inline=False
        )
    else:
        embed.add_field(name="Handled by", value=resolver or "Automatically", inline=True)
        if note:
            embed.add_field(name="Note", value=note[:500], inline=False)
    return embed


async def _finish_resolution(interaction, new_status, note, message):
    """Shared by the Fulfill button and the Deny modal."""
    if not _is_mod(interaction.user):
        await interaction.response.send_message("⚠️ Only mods can handle redemption requests.", ephemeral=True)
        return
    red = get_redemption_by_message(message.id)
    if not red:
        await interaction.response.send_message("⚠️ I can't find that request in my records.", ephemeral=True)
        return

    ok, text, row = resolve_redemption(red["id"], new_status, interaction.user.id, note, _now())
    if not ok:
        await interaction.response.send_message(f"⚠️ {text}", ephemeral=True)
        return

    ev = get_event(row["event_id"])
    embed = _request_embed(row, ev["name"] if ev else "?", new_status, resolver=interaction.user.mention, note=note)
    try:
        await message.edit(embed=embed, view=None)
    except Exception as e:
        print(f"⚠️ Points: couldn't update request message #{red['id']}: {e}")

    verb = "fulfilled" if new_status == "fulfilled" else "denied and refunded"
    await interaction.response.send_message(f"Request `#{red['id']}` {verb}.", ephemeral=True)

    if new_status == "fulfilled":
        await _notify_user(row["user_id"],
            f"🎁 Your prize **{row['item_name']}** from **{ev['name'] if ev else 'the event'}** has been marked as fulfilled. "
            f"If you haven't received it in Neopets, let a mod know!")
    else:
        extra = f" Reason: {note}" if note else ""
        await _notify_user(row["user_id"],
            f"Your redemption for **{row['item_name']}** couldn't be completed, so your {row['cost']:,} points were refunded.{extra}")


class DenyModal(discord.ui.Modal, title="Deny + refund"):
    reason = discord.ui.TextInput(
        label="Reason (optional, the member will see it)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, source_message):
        super().__init__()
        self.source_message = source_message

    async def on_submit(self, interaction: discord.Interaction):
        await _finish_resolution(interaction, "denied", (self.reason.value or "").strip(), self.source_message)


class RedemptionRequestView(discord.ui.View):
    """Persistent buttons - they keep working after the bot restarts, because the
    request is looked up by the message the buttons are attached to."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fulfill", style=discord.ButtonStyle.success, emoji="✅", custom_id="pt_fulfill")
    async def fulfill(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _finish_resolution(interaction, "fulfilled", None, interaction.message)

    @discord.ui.button(label="Deny + refund", style=discord.ButtonStyle.danger, emoji="❌", custom_id="pt_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_mod(interaction.user):
            await interaction.response.send_message("⚠️ Only mods can handle redemption requests.", ephemeral=True)
            return
        await interaction.response.send_modal(DenyModal(interaction.message))


async def _process_redeem(guild, member, item_id, now=None):
    """Does a whole redemption. Returns (success, text to show the member)."""
    now = now or _now()
    channel = _find_request_channel(guild)
    if channel is None:
        return (False, "⚠️ I can't find the mods' request channel, so I can't take redemptions right now. Please let a mod know.")

    ok, text, data = attempt_redeem(member.id, item_id, now)
    if not ok:
        return (False, f"⚠️ {text}")

    red_id, item, ev = data["redemption_id"], data["item"], data["event"]

    # Role prizes are granted instantly
    auto_done = False
    if item["role_id"]:
        role = guild.get_role(item["role_id"])
        if role is not None:
            try:
                await member.add_roles(role, reason=f"Event prize: {item['name']} ({ev['name']})")
                resolve_redemption(red_id, "fulfilled", 0, "Role granted automatically", now)
                auto_done = True
            except Exception as e:
                print(f"⚠️ Points: couldn't grant role for redemption #{red_id}: {e}")

    red = get_redemption(red_id)
    try:
        if red["status"] == "pending":
            msg = await channel.send(embed=_request_embed(red, ev["name"], "pending"), view=RedemptionRequestView())
        else:
            msg = await channel.send(embed=_request_embed(red, ev["name"], "fulfilled", note=red["note"]))
        _set_request_ref(red_id, guild.id, channel.id, msg.id)
    except Exception as e:
        print(f"⚠️ Points: couldn't post redemption request #{red_id}: {e}")
        if red["status"] == "pending":
            resolve_redemption(red_id, "denied", 0, "Couldn't reach the mods' channel, so this was refunded automatically", now)
            return (False, "⚠️ I couldn't reach the mods' channel, so your points were refunded. Please try again or ping a mod.")

    if auto_done:
        return (True, f"🎉 You redeemed **{item['name']}** and the role has been added! You have **{data['balance']:,}** points left.")
    if item["role_id"]:
        return (True, f"✅ You redeemed **{item['name']}**. I couldn't add the role automatically, so a mod will sort it out. "
                      f"You have **{data['balance']:,}** points left.")
    return (True, f"✅ You redeemed **{item['name']}**! A mod will deliver it to you in Neopets and mark it fulfilled — "
                  f"you'll get a DM. You have **{data['balance']:,}** points left.")


class ConfirmRedeemView(discord.ui.View):
    def __init__(self, user_id, item_id):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.item_id = item_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your redemption.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="⏳ Processing...", view=None)
        ok, text = await _process_redeem(interaction.guild, interaction.user, self.item_id)
        await interaction.edit_original_response(content=text)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your redemption.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="Cancelled — no points were spent.", view=None)


# ============================================================
#  MEMBER COMMANDS
# ============================================================

@app_commands.command(name="points", description="Check your event points balance")
@app_commands.describe(member="[Mod] Look up another member instead of yourself")
@app_commands.guild_only()
async def points_cmd(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if await _deny_wrong_guild(interaction):
        return
    target = member or interaction.user
    if target.id != interaction.user.id and not _is_mod(interaction.user):
        await interaction.response.send_message("⚠️ You can only look up your own points.", ephemeral=True)
        return

    now = _now()
    events = open_events(now)
    if not events:
        upcoming = [e for e in list_events() if event_phase(e, now) == "scheduled"]
        text = "There's no points event running right now."
        if upcoming:
            nxt = min(upcoming, key=lambda e: e["earn_start"])
            text += f"\nComing up: **{nxt['name']}**, starting {_ts(nxt['earn_start'], 'R')}."
        await interaction.response.send_message(text, ephemeral=True)
        return

    embeds = []
    for ev in events:
        phase = event_phase(ev, now)
        with _db() as conn:
            balance = _balance(conn, ev["id"], target.id)
            earned_today = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS t FROM pt_ledger "
                "WHERE event_id = ? AND user_id = ? AND kind = 'auto' AND created_at >= ?",
                (ev["id"], target.id, _iso(_pacific_day_start(now)))
            ).fetchone()["t"]
            recent = conn.execute(
                "SELECT * FROM pt_ledger WHERE event_id = ? AND user_id = ? AND kind != 'auto' ORDER BY id DESC LIMIT 5",
                (ev["id"], target.id)
            ).fetchall()

        lines = [f"**Balance: {_pts(balance)}**", _time_left_text(ev, phase)]
        if phase == "earning" and ev["auto_points"]:
            lines.append(f"Chat points today: {earned_today}/{ev['daily_cap']}")
            chans, cats = get_event_channels(ev["id"])
            if chans or cats:
                lines.append(f"📍 Earn in: {channels_text(ev['id'])}")
        if recent:
            lines.append("\n**Recent activity**")
            for r in recent:
                sign = "+" if r["amount"] > 0 else ""
                lines.append(f"`{sign}{r['amount']}` {r['reason'] or r['kind']} · {_ts(r['created_at'], 'R')}")
        embeds.append(discord.Embed(
            title=f"{ev['name']} — {PHASE_LABELS[phase]}",
            description="\n".join(lines),
            color=EMBED_COLOR
        ))

    who = "" if target.id == interaction.user.id else f"Points for {target.display_name}:"
    await interaction.response.send_message(content=who or None, embeds=embeds[:10], ephemeral=True)


@app_commands.command(name="shop", description="Browse the prizes you can spend your event points on")
@app_commands.guild_only()
async def shop_cmd(interaction: discord.Interaction):
    if await _deny_wrong_guild(interaction):
        return
    now = _now()
    events = open_events(now)
    if not events:
        await interaction.response.send_message("The prize shop isn't open right now.", ephemeral=True)
        return

    embeds = []
    for ev in events:
        phase = event_phase(ev, now)
        items = items_for_event(ev["id"])
        balance = get_balance(ev["id"], interaction.user.id)
        if items:
            text = "\n\n".join(_item_line(it) for it in items)
            if len(text) > 3800:
                text = text[:3800] + "\n…"
        else:
            text = "No prizes have been added yet — check back soon!"
        embed = discord.Embed(title=f"🛍️ {ev['name']} shop", description=text, color=EMBED_COLOR)
        embed.set_footer(text=f"Your balance: {balance:,} points")
        embed.add_field(name="Shop status", value=_time_left_text(ev, phase), inline=False)
        embeds.append(embed)

    await interaction.response.send_message(
        content="Claim a prize with `/redeem`.", embeds=embeds[:10], ephemeral=True
    )


@app_commands.command(name="redeem", description="Spend your event points on a prize")
@app_commands.describe(prize="Pick a prize from the list")
@app_commands.guild_only()
async def redeem_cmd(interaction: discord.Interaction, prize: int):
    if await _deny_wrong_guild(interaction):
        return
    now = _now()
    item = get_item(prize)
    ev = get_event(item["event_id"]) if item else None
    if not item or not item["active"] or not ev or event_phase(ev, now) not in ("earning", "shop"):
        await interaction.response.send_message("⚠️ That prize isn't available right now.", ephemeral=True)
        return

    if item["stock"] is not None and item["stock"] <= 0:
        await interaction.response.send_message(f"⚠️ **{item['name']}** is sold out.", ephemeral=True)
        return
    if item["per_user_limit"] is not None:
        with _db() as conn:
            claimed = conn.execute(
                "SELECT COUNT(*) AS n FROM pt_redemptions WHERE user_id = ? AND item_id = ? AND status != 'denied'",
                (interaction.user.id, item["id"])
            ).fetchone()["n"]
        if claimed >= item["per_user_limit"]:
            await interaction.response.send_message(
                f"⚠️ You've already claimed the limit for **{item['name']}** ({item['per_user_limit']} per member).",
                ephemeral=True
            )
            return

    balance = get_balance(ev["id"], interaction.user.id)
    if balance < item["cost"]:
        await interaction.response.send_message(
            f"⚠️ **{item['name']}** costs {item['cost']:,} points, but you only have {balance:,}.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Spend **{item['cost']:,} points** on **{item['name']}** (from {ev['name']})?\n"
        f"You'd have {balance - item['cost']:,} left.",
        view=ConfirmRedeemView(interaction.user.id, item["id"]),
        ephemeral=True
    )


@redeem_cmd.autocomplete("prize")
async def redeem_autocomplete(interaction: discord.Interaction, current: str):
    now = _now()
    choices = []
    for ev in open_events(now):
        for it in items_for_event(ev["id"]):
            if it["stock"] is not None and it["stock"] <= 0:
                continue
            if current.lower() in it["name"].lower():
                label = f"{it['name']} — {it['cost']:,} pts ({ev['name']})"
                choices.append(app_commands.Choice(name=label[:100], value=it["id"]))
    return choices[:25]


# ============================================================
#  MOD COMMANDS: awarding / removing points
# ============================================================

@app_commands.command(name="awardpoints", description="[Mod] Give a member bonus event points")
@app_commands.describe(member="Who gets the points", amount="How many points", reason="What they're being rewarded for")
@app_commands.guild_only()
async def awardpoints_cmd(interaction: discord.Interaction, member: discord.Member,
                          amount: app_commands.Range[int, 1, MAX_AWARD],
                          reason: app_commands.Range[str, 1, 200]):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    if member.bot:
        await interaction.response.send_message("⚠️ Bots can't earn points.", ephemeral=True)
        return
    if member.id == interaction.user.id and not POINTS_ALLOW_SELF_AWARD:
        await interaction.response.send_message("⚠️ You can't award points to yourself — ask another mod.", ephemeral=True)
        return

    now = _now()
    ev = _target_event_for_mod(now)
    if not ev:
        await interaction.response.send_message(
            "⚠️ No event is earning points right now. Points can only be awarded while an event is running "
            "(start one with `/event start`).", ephemeral=True
        )
        return

    with _db() as conn:
        _add_ledger(conn, ev["id"], member.id, amount, "award", reason, interaction.user.id, now)
        conn.commit()
        new_balance = _balance(conn, ev["id"], member.id)

    await interaction.response.send_message(
        f"🎁 {member.mention} was awarded **{_pts(amount)}** in **{ev['name']}** for: {reason}\n"
        f"*(They now have {new_balance:,}.)*",
        ephemeral=not POINTS_PUBLIC_AWARDS
    )


@app_commands.command(name="removepoints", description="[Mod] Take event points away from a member")
@app_commands.describe(member="Whose points to reduce", amount="How many points to remove", reason="Why")
@app_commands.guild_only()
async def removepoints_cmd(interaction: discord.Interaction, member: discord.Member,
                           amount: app_commands.Range[int, 1, MAX_AWARD],
                           reason: app_commands.Range[str, 1, 200]):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    now = _now()
    ev = _event_for_removal(now)
    if not ev:
        await interaction.response.send_message("⚠️ There's no running event to remove points from.", ephemeral=True)
        return

    with _db() as conn:
        balance = _balance(conn, ev["id"], member.id)
        if amount > balance:
            await interaction.response.send_message(
                f"⚠️ {member.display_name} only has {balance:,} points in **{ev['name']}**.", ephemeral=True
            )
            return
        _add_ledger(conn, ev["id"], member.id, -amount, "remove", reason, interaction.user.id, now)
        conn.commit()

    await interaction.response.send_message(
        f"Removed **{_pts(amount)}** from {member.display_name} in **{ev['name']}** (now {balance - amount:,}). "
        f"Reason logged: {reason}",
        ephemeral=True
    )


@app_commands.command(name="redemptions", description="[Mod] List redemption requests still waiting for a mod")
@app_commands.guild_only()
async def redemptions_cmd(interaction: discord.Interaction):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    with _db() as conn:
        rows = conn.execute(
            "SELECT r.*, e.name AS event_name FROM pt_redemptions r JOIN pt_events e ON e.id = r.event_id "
            "WHERE r.status = 'pending' ORDER BY r.id LIMIT 25"
        ).fetchall()
    if not rows:
        await interaction.response.send_message("✅ Nothing is waiting — all redemption requests are handled.", ephemeral=True)
        return
    lines = []
    for r in rows:
        link = ""
        if r["request_guild_id"] and r["request_channel_id"] and r["request_message_id"]:
            link = f" — [jump](https://discord.com/channels/{r['request_guild_id']}/{r['request_channel_id']}/{r['request_message_id']})"
        lines.append(f"`#{r['id']}` <@{r['user_id']}> — **{r['item_name']}** ({r['event_name']}){link}")
    await interaction.response.send_message("**Waiting on a mod:**\n" + "\n".join(lines), ephemeral=True)


# ============================================================
#  MOD COMMANDS: /event
# ============================================================

event_group = app_commands.Group(name="event", description="[Mod] Run points events", guild_only=True)


class ChannelPickerView(discord.ui.View):
    """Ephemeral picker a mod uses to choose where an event's points can be earned."""

    def __init__(self, user_id, event_id):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.event_id = event_id
        # ChannelSelect needs discord.py 2.1+. Built here (not as a decorator) so that an
        # older version just loses the dropdown instead of crashing the whole bot on startup.
        self.pick = None
        if hasattr(discord.ui, "ChannelSelect"):
            self.pick = discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text, discord.ChannelType.news,
                               discord.ChannelType.forum, discord.ChannelType.category],
                placeholder="Pick up to 25 channels or categories…",
                min_values=1, max_values=25,
            )
            self.pick.callback = self._on_pick
            self.add_item(self.pick)

    def message_text(self):
        ev = get_event(self.event_id)
        name = ev["name"] if ev else "this event"
        return (
            f"📍 **Where can points be earned during {name}?**\n"
            f"Right now: {channels_text(self.event_id)}\n\n"
            f"Pick channels below — your choice **replaces** the current list. Picking a category covers every "
            f"channel in it, and posts inside threads count as their channel. "
            f"Leave this alone (or press the button) to allow most channels."
        )

    async def _authorized(self, interaction):
        if interaction.user.id != self.user_id or not _is_mod(interaction.user):
            await interaction.response.send_message("⚠️ This picker belongs to the mod who opened it.", ephemeral=True)
            return False
        ev = get_event(self.event_id)
        if not ev or event_phase(ev, _now()) not in ("scheduled", "earning"):
            await interaction.response.send_message(
                "⚠️ That event isn't earning (or about to) any more, so its channels can't be changed.", ephemeral=True
            )
            return False
        return True

    async def apply_picks(self, interaction, values):
        if not await self._authorized(interaction):
            return
        picks = [
            (c.id, "category" if getattr(c, "type", None) == discord.ChannelType.category else "channel")
            for c in values
        ]
        set_event_channels(self.event_id, picks)
        await interaction.response.edit_message(content=self.message_text(), view=self)
        await _refresh_open_announcement(self.event_id)

    async def _on_pick(self, interaction: discord.Interaction):
        await self.apply_picks(interaction, list(self.pick.values))

    @discord.ui.button(label="Use all channels", style=discord.ButtonStyle.secondary, emoji="🌐")
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply_picks(interaction, [])


def _parse_start_date(text, now):
    """'YYYY-MM-DD' -> midnight Pacific that day (never earlier than now)."""
    if not text or not text.strip():
        return now
    d = datetime.strptime(text.strip(), "%Y-%m-%d")
    return max(d.replace(tzinfo=PACIFIC).astimezone(timezone.utc), now)


async def event_choices(interaction, current, phases):
    if not _is_mod(interaction.user):
        return []
    now = _now()
    out = []
    for ev in list_events():
        phase = event_phase(ev, now)
        if phase in phases and current.lower() in ev["name"].lower():
            out.append(app_commands.Choice(name=f"#{ev['id']} {ev['name']} ({phase})"[:100], value=ev["id"]))
    return out[:25]


@event_group.command(name="start", description="[Mod] Create a points event (starts now unless you give a start date)")
@app_commands.describe(
    name="What the event is called",
    earn_days="How many days members can earn points",
    shop_days="Days the shop stays open after earning ends (default 3)",
    start_date="Optional start day as YYYY-MM-DD (Pacific). Leave blank to start now",
    auto_points="Give points automatically for chatting? (default yes)",
    points_per_message="Points per scoring message (default 1)",
    daily_cap="Most automatic points one member can earn per day (default 10)",
    announce_channel="Where to announce the event (default: this channel)",
)
async def event_start(interaction: discord.Interaction,
                      name: app_commands.Range[str, 1, 60],
                      earn_days: app_commands.Range[int, 1, 120],
                      shop_days: app_commands.Range[int, 1, 30] = DEFAULT_SHOP_DAYS,
                      start_date: Optional[str] = None,
                      auto_points: bool = True,
                      points_per_message: app_commands.Range[int, 1, 50] = DEFAULT_POINTS_PER_MESSAGE,
                      daily_cap: app_commands.Range[int, 1, 1000] = DEFAULT_DAILY_CAP,
                      announce_channel: Optional[discord.TextChannel] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return

    now = _now()
    try:
        start_dt = _parse_start_date(start_date, now)
    except ValueError:
        await interaction.response.send_message("⚠️ Start date should look like `2026-12-01`.", ephemeral=True)
        return

    channel = announce_channel
    if channel is None and POINTS_ANNOUNCE_CHANNEL:
        channel = discord.utils.get(interaction.guild.text_channels, name=POINTS_ANNOUNCE_CHANNEL)
    if channel is None:
        channel = interaction.channel

    ok, event_id_or_name, start, end, close = create_event(
        name.strip(), interaction.user.id, earn_days, shop_days, start_dt, auto_points,
        points_per_message, daily_cap, getattr(channel, "id", None), now
    )
    if not ok:
        await interaction.response.send_message(
            f"⚠️ That would overlap the earning period of **{event_id_or_name}**. Only one event can be earning at a time.",
            ephemeral=True
        )
        return

    embed = discord.Embed(title=f"✅ Event created: {name.strip()}", color=EMBED_COLOR)
    embed.add_field(name="Earning", value=f"{_ts(_iso(start))} → {_ts(_iso(end))}", inline=False)
    embed.add_field(name="Shop open until", value=f"{_ts(_iso(close))} ({shop_days} day(s) after earning ends)", inline=False)
    embed.add_field(
        name="Automatic points",
        value=(f"{points_per_message}/message, max {daily_cap}/day" if auto_points else "Off — mods award points manually"),
        inline=False
    )
    embed.add_field(name="Announcements go to", value=getattr(channel, "mention", "this channel"), inline=False)
    send_kwargs = {"embed": embed, "ephemeral": True}
    if auto_points:
        embed.add_field(
            name="Where points can be earned",
            value="Most channels for now — use the dropdown below to choose specific ones.",
            inline=False
        )
        send_kwargs["view"] = ChannelPickerView(interaction.user.id, event_id_or_name)
    embed.set_footer(text=f"Event #{event_id_or_name} · Add prizes with /shopmod add")
    await interaction.response.send_message(**send_kwargs)

    await run_tick()  # posts the opening announcement right away if the event starts now


@event_group.command(name="channels", description="[Mod] Choose which channels earn points for an event")
@app_commands.describe(event="Which event (default: the current or next one)")
async def event_channels(interaction: discord.Interaction, event: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    now = _now()
    if event is not None:
        ev = get_event(event)
    else:
        candidates = [e for e in list_events() if event_phase(e, now) in ("earning", "scheduled")]
        candidates.sort(key=lambda e: (0 if event_phase(e, now) == "earning" else 1, e["earn_start"]))
        ev = candidates[0] if candidates else None
    if not ev or event_phase(ev, now) not in ("scheduled", "earning"):
        await interaction.response.send_message(
            "⚠️ There's no current or upcoming event whose channels can be set.", ephemeral=True
        )
        return
    view = ChannelPickerView(interaction.user.id, ev["id"])
    await interaction.response.send_message(content=view.message_text(), view=view, ephemeral=True)


@event_group.command(name="list", description="[Mod] Show recent and upcoming events")
async def event_list(interaction: discord.Interaction):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    events = list_events(10)
    if not events:
        await interaction.response.send_message("No events yet. Create one with `/event start`.", ephemeral=True)
        return
    now = _now()
    lines = []
    for ev in events:
        phase = event_phase(ev, now)
        lines.append(
            f"`#{ev['id']}` **{ev['name']}** — {PHASE_LABELS[phase]}\n"
            f"  earning {_ts(ev['earn_start'], 'd')} → {_ts(ev['earn_end'], 'd')} · shop until {_ts(ev['shop_close'], 'd')}"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@event_group.command(name="end", description="[Mod] Stop earning now; the shop stays open for its normal shop days")
@app_commands.describe(event="Which event")
async def event_end(interaction: discord.Interaction, event: int):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    ok, why = end_event_now(event, _now())
    if not ok:
        msg = "That event doesn't exist." if why == "missing" else f"That event isn't earning right now (it's **{why}**)."
        await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)
        return
    await interaction.response.send_message("✅ Earning has ended. The shop is open for its shop days.", ephemeral=True)
    await run_tick()


@event_group.command(name="close", description="[Mod] Close the shop right now (unspent points expire)")
@app_commands.describe(event="Which event")
async def event_close(interaction: discord.Interaction, event: int):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    ok, why = close_event_now(event, _now())
    if not ok:
        msg = "That event doesn't exist." if why == "missing" else f"That event's shop isn't open (it's **{why}**)."
        await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)
        return
    await interaction.response.send_message("🔒 The shop is closed and unspent points have expired.", ephemeral=True)
    await run_tick()


@event_group.command(name="cancel", description="[Mod] Cancel an event that hasn't started yet")
@app_commands.describe(event="Which event")
async def event_cancel(interaction: discord.Interaction, event: int):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    ok, why = cancel_event(event, _now())
    if not ok:
        msg = "That event doesn't exist." if why == "missing" else f"Only events that haven't started can be cancelled (this one is **{why}**)."
        await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)
        return
    await interaction.response.send_message("🚫 Event cancelled.", ephemeral=True)


@event_channels.autocomplete("event")
async def _ac_event_channels(interaction, current: str):
    return await event_choices(interaction, current, ("earning", "scheduled"))


@event_end.autocomplete("event")
async def _ac_event_end(interaction, current: str):
    return await event_choices(interaction, current, ("earning",))


@event_close.autocomplete("event")
async def _ac_event_close(interaction, current: str):
    return await event_choices(interaction, current, ("earning", "shop"))


@event_cancel.autocomplete("event")
async def _ac_event_cancel(interaction, current: str):
    return await event_choices(interaction, current, ("scheduled",))


# ============================================================
#  MOD COMMANDS: /shopmod  (managing prizes)
# ============================================================

shopmod_group = app_commands.Group(name="shopmod", description="[Mod] Manage the prize shop", guild_only=True)


def _default_event_for_items(now):
    """Where a new prize goes if the mod doesn't pick: the earning event, else the
    next upcoming one, else the one in its shop phase."""
    events = [e for e in list_events() if event_phase(e, now) in ("earning", "scheduled", "shop")]
    order = {"earning": 0, "scheduled": 1, "shop": 2}
    events.sort(key=lambda e: (order[event_phase(e, now)], e["earn_start"]))
    return events[0] if events else None


async def _item_choices(interaction, current, only_open):
    if not _is_mod(interaction.user):
        return []
    now = _now()
    out = []
    for ev in list_events():
        phase = event_phase(ev, now)
        if phase == "closed" or (only_open and phase not in ("earning", "shop")):
            continue
        for it in items_for_event(ev["id"]):
            if current.lower() in it["name"].lower():
                out.append(app_commands.Choice(name=f"{it['name']} — {it['cost']:,} pts ({ev['name']})"[:100], value=it["id"]))
    return out[:25]


@shopmod_group.command(name="add", description="[Mod] Add a prize to an event's shop")
@app_commands.describe(
    name="Prize name",
    cost="Price in points",
    stock="How many are available (leave blank for unlimited)",
    limit_per_member="Most one member can claim (leave blank for no limit)",
    role="Make this prize a Discord role that's granted instantly",
    description="Short description shown in the shop",
    event="Which event (default: the current or next one)",
)
async def shopmod_add(interaction: discord.Interaction,
                      name: app_commands.Range[str, 1, 80],
                      cost: app_commands.Range[int, 1, 1000000],
                      stock: Optional[app_commands.Range[int, 1, 100000]] = None,
                      limit_per_member: Optional[app_commands.Range[int, 1, 1000]] = None,
                      role: Optional[discord.Role] = None,
                      description: Optional[app_commands.Range[str, 0, 200]] = None,
                      event: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    now = _now()
    ev = get_event(event) if event is not None else _default_event_for_items(now)
    if not ev or ev["cancelled"] or event_phase(ev, now) == "closed":
        await interaction.response.send_message(
            "⚠️ There's no current or upcoming event to add a prize to. Create one with `/event start`.", ephemeral=True
        )
        return

    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO pt_items (event_id, name, description, cost, stock, per_user_limit, role_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev["id"], name.strip(), (description or "").strip(), cost, stock, limit_per_member,
             role.id if role else None, _iso(now))
        )
        conn.commit()
        item_id = cur.lastrowid

    await interaction.response.send_message(
        f"✅ Added prize `#{item_id}` **{name.strip()}** ({cost:,} pts) to **{ev['name']}**.", ephemeral=True
    )


@shopmod_group.command(name="edit", description="[Mod] Change a prize's name, price, description, or limit")
@app_commands.describe(prize="Which prize", name="New name", cost="New price (only affects future redemptions)",
                       description="New description", limit_per_member="New per-member limit")
async def shopmod_edit(interaction: discord.Interaction, prize: int,
                       name: Optional[app_commands.Range[str, 1, 80]] = None,
                       cost: Optional[app_commands.Range[int, 1, 1000000]] = None,
                       description: Optional[app_commands.Range[str, 0, 200]] = None,
                       limit_per_member: Optional[app_commands.Range[int, 1, 1000]] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    it = get_item(prize)
    if not it:
        await interaction.response.send_message("⚠️ I can't find that prize.", ephemeral=True)
        return
    if name is None and cost is None and description is None and limit_per_member is None:
        await interaction.response.send_message("⚠️ Tell me what to change.", ephemeral=True)
        return
    with _db() as conn:
        conn.execute(
            "UPDATE pt_items SET name = ?, cost = ?, description = ?, per_user_limit = ? WHERE id = ?",
            (name.strip() if name else it["name"],
             cost if cost is not None else it["cost"],
             description.strip() if description is not None else it["description"],
             limit_per_member if limit_per_member is not None else it["per_user_limit"],
             prize)
        )
        conn.commit()
    await interaction.response.send_message(f"✅ Updated prize `#{prize}`.", ephemeral=True)


@shopmod_group.command(name="restock", description="[Mod] Add more copies of a limited prize")
@app_commands.describe(prize="Which prize", amount="How many more to add")
async def shopmod_restock(interaction: discord.Interaction, prize: int, amount: app_commands.Range[int, 1, 100000]):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    it = get_item(prize)
    if not it:
        await interaction.response.send_message("⚠️ I can't find that prize.", ephemeral=True)
        return
    if it["stock"] is None:
        await interaction.response.send_message("⚠️ That prize already has unlimited stock.", ephemeral=True)
        return
    with _db() as conn:
        conn.execute("UPDATE pt_items SET stock = stock + ? WHERE id = ?", (amount, prize))
        conn.commit()
    await interaction.response.send_message(f"✅ Added {amount} — **{it['name']}** now has {it['stock'] + amount} in stock.", ephemeral=True)


@shopmod_group.command(name="remove", description="[Mod] Take a prize off the shop (pending requests are unaffected)")
@app_commands.describe(prize="Which prize")
async def shopmod_remove(interaction: discord.Interaction, prize: int):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    it = get_item(prize)
    if not it:
        await interaction.response.send_message("⚠️ I can't find that prize.", ephemeral=True)
        return
    with _db() as conn:
        conn.execute("UPDATE pt_items SET active = 0 WHERE id = ?", (prize,))
        conn.commit()
    await interaction.response.send_message(f"✅ **{it['name']}** is no longer in the shop.", ephemeral=True)


@shopmod_group.command(name="list", description="[Mod] Show every prize, including sold-out and removed ones")
async def shopmod_list(interaction: discord.Interaction):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    now = _now()
    blocks = []
    for ev in list_events(10):
        phase = event_phase(ev, now)
        if phase == "closed":
            continue
        items = items_for_event(ev["id"], only_active=False)
        lines = []
        for it in items:
            tag = "" if it["active"] else " *(removed)*"
            lines.append(_item_line(it).split("\n")[0] + tag)
        blocks.append(f"**{ev['name']}** ({PHASE_LABELS[phase]})\n" + ("\n".join(lines) if lines else "*no prizes yet*"))
    if not blocks:
        await interaction.response.send_message("No open or upcoming events.", ephemeral=True)
        return
    text = "\n\n".join(blocks)
    await interaction.response.send_message(text[:1900], ephemeral=True)


@shopmod_add.autocomplete("event")
async def _ac_shopmod_add_event(interaction, current: str):
    return await event_choices(interaction, current, ("earning", "scheduled", "shop"))


@shopmod_edit.autocomplete("prize")
async def _ac_edit_prize(interaction, current: str):
    return await _item_choices(interaction, current, only_open=False)


@shopmod_restock.autocomplete("prize")
async def _ac_restock_prize(interaction, current: str):
    return await _item_choices(interaction, current, only_open=False)


@shopmod_remove.autocomplete("prize")
async def _ac_remove_prize(interaction, current: str):
    return await _item_choices(interaction, current, only_open=False)


# ============================================================
#  SETUP - called once from main.py
# ============================================================

def setup(bot, get_db, mod_roles, main_guild_name, excluded_channels, request_channel_name):
    global _bot, _get_db_fn, _mod_roles, _guild_names, _excluded_channels, _request_channel_name
    _bot = bot
    _get_db_fn = get_db
    _mod_roles = list(mod_roles)
    _guild_names = [main_guild_name] + list(POINTS_GUILD_NAMES_EXTRA)
    _request_channel_name = request_channel_name
    _excluded_channels = set(excluded_channels) | {request_channel_name}

    init_tables()

    for command in (points_cmd, shop_cmd, redeem_cmd, awardpoints_cmd, removepoints_cmd,
                    redemptions_cmd, event_group, shopmod_group):
        bot.tree.add_command(command)
    print("🎟️ Points system ready")


def start_background(bot):
    """Call from on_ready: re-attaches the Fulfill/Deny buttons so they work on
    old requests after a restart, and starts the announcements loop."""
    bot.add_view(RedemptionRequestView())
    bot.loop.create_task(points_loop())
