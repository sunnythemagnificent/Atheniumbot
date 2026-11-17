"""
santa.py - Secret Santa / gift & art exchanges for the guild
============================================================

HOW AN EXCHANGE RUNS
1. A mod starts one:           /santa start      (gift, NP, NC, art, or other). Several can run AT THE SAME TIME
                               (say an art exchange, an NP exchange and an NC exchange), and one member
                               can sign up for as many of them as they like. When more than one is running,
                               commands take an `exchange` option (pick it from the list that pops up).
   It can open right away, or on a day you choose (opens_on): the bot then posts the announcement by itself,
   and when the sign-up deadline passes it posts a "sign-ups are closed" note and tells the mods.
2. Members sign up:            the 🎁 button on the announcement (a pop-up form), or on the website,
                               which gives a short code to redeem here with /santa join code:XXXX
   Each person shares a wishlist (gift exchange) or the characters they'd like drawn (art exchange),
   plus optional links and things to avoid. Only their Secret Santa ever sees it.
3. A mod draws the matches:    /santa draw       (everyone gets a giftee; nobody gets themselves)
4. Everyone checks their match with the 🎅 button (only they can see it), sends their gift or art,
   and marks it 📦 sent. Receivers mark 💌 received (with an optional thank-you note).
5. A mod reveals the pairs:    /santa reveal     then   /santa close  (which erases wishlists and notes)

WHO CAN SEE WHAT
  - Members see only their OWN giftee. Nobody is told who their Santa is.
  - The website only ever receives the event info and counts. Matches are never stored there; they
    appear on the website only if a mod runs /santa reveal.
  - Mods can look up one member's match in an emergency (/santa lookup). Every lookup is written to
    the mod channel, so it is never a secret one.

HOOKS NEEDED IN main.py (3 small edits):
    import santa                                                                (near the top)
    santa.setup(bot, get_db, BOT_MOD_ROLES, MAIN_GUILD_NAME, STRIKE_ALERT_CHANNEL)   (just before bot.run)
    santa.start_background(bot)                                                 (in on_ready, with the other tasks)
"""

import asyncio
import os
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

# ============================================================
#  CONFIGURATION - edit these values
# ============================================================

# The main server's name is added automatically. Add a test server's exact name to try it there.
SANTA_GUILD_NAMES_EXTRA = []

MIN_PLAYERS = 3                # an exchange needs at least this many people
MAX_PLAYERS = 100

WANTS_MAX = 800                # wishlist / character info
LINKS_MAX_COUNT = 5            # how many links a person can share
AVOID_MAX = 400
THANKS_MAX = 300
RULES_MAX = 1500
BUDGET_MAX = 200

REMINDER_DAYS = (7, 2)         # givers who haven't marked their gift sent are reminded this many days before the deadline
LOOP_SECONDS = 60
WEB_PUSH_MIN_SECONDS = 30      # at most one website update this often while people are signing up
CLAIM_ATTEMPTS = 5             # website codes a member may try...
CLAIM_WINDOW_SECONDS = 600     # ...per this many seconds

# The public page on your website. It reuses ACTIVITY_SYNC_SECRET, like your other website syncs.
# Set SANTA_WEB_SYNC_URL to an empty string to switch the website side off entirely.
WEB_SYNC_URL = os.environ.get("SANTA_WEB_SYNC_URL", "https://athenaeumarchive.com/santa/santa_sync.php")
WEB_CLAIM_URL = os.environ.get("SANTA_CLAIM_URL", "https://athenaeumarchive.com/santa/santa_claim.php")
WEB_PUBLIC_URL = os.environ.get("SANTA_PUBLIC_URL", "https://athenaeumarchive.com/santa/")
WEB_SECRET_ENV = "ACTIVITY_SYNC_SECRET"

EMBED_COLOR = 0xB5303A
PACIFIC = ZoneInfo("America/Los_Angeles")

# Exchange types. They set the wording on the sign-up form.
KINDS = {
    "gift": {"label": "Gift exchange", "noun": "gift",
             "wants_label": "Wishlist: items, NP, or themes you'd love",
             "wants_hint": "Things you'd love to get, items, NP, or themes. A few ideas help your Santa.",
             "links_label": "Wishlist links (one per line)",
             "avoid_label": "Please avoid (things you dislike)"},
    "art": {"label": "Art exchange", "noun": "piece of art",
            "wants_label": "Character(s) to draw & what you'd like",
            "wants_hint": "Who should your Santa draw? Names, descriptions, poses, moods, anything you'd love to see.",
            "links_label": "Reference links (one per line)",
            "avoid_label": "Please avoid (content, styles, ratings)"},
    "np": {"label": "NP exchange", "noun": "NP gift",
           "wants_label": "Items or themes you'd love (NP)",
           "wants_hint": "What would you love to open? Item types, themes, favorite colors, pets or characters to match.",
           "links_label": "Wishlist links (one per line)",
           "avoid_label": "Please avoid (things you dislike)"},
    "nc": {"label": "NC exchange", "noun": "NC gift",
           "wants_label": "NC items or themes you'd love",
           "wants_hint": "Which NC items or themes would you love? Wearables, species or colors you collect, things you already own.",
           "links_label": "Wishlist links (one per line)",
           "avoid_label": "Please avoid (items you own, dislikes)"},
    "other": {"label": "Exchange", "noun": "present",
              "wants_label": "What would you like?",
              "wants_hint": "What would make your Santa's job easy?",
              "links_label": "Links (one per line)",
              "avoid_label": "Please avoid"},
}

# ============================================================
#  STATE (filled in by setup())
# ============================================================

_bot = None
_get_db_fn = None
_mod_roles = []
_guild_names = []
_mod_channel_name = None
_claim_attempts = {}            # user id -> times of recent website-code tries
_last_web_push = {}             # event id -> when the website was last updated


def _now():
    return datetime.now(timezone.utc)


_FMT = "%Y-%m-%d %H:%M:%S"


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime(_FMT)


def _parse(s):
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


def _ts(s, style="f"):
    return f"<t:{int(_parse(s).timestamp())}:{style}>"


def _end_of_pacific_day(text):
    """'2026-12-01' -> 11:59:59 PM Pacific that day, as UTC. Raises ValueError for anything else."""
    return datetime.strptime(text.strip(), "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=PACIFIC).astimezone(timezone.utc)


def parse_pacific(text, default_time=(23, 59, 59)):
    """'2026-12-01' (uses default_time) or '2026-12-01 18:30' or '2026-12-01 6:30 PM', all Pacific time.
    Returns UTC. Raises ValueError for anything else."""
    text = " ".join(str(text).strip().upper().split())
    try:
        local = datetime.strptime(text, "%Y-%m-%d").replace(hour=default_time[0], minute=default_time[1], second=default_time[2])
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %I %p", "%Y-%m-%d %I%p"):
            try:
                local = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError("bad date")
    return local.replace(tzinfo=PACIFIC).astimezone(timezone.utc)


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
    """A write transaction that locks the database up front, so two people acting at the same
    instant can't slip past each other's checks."""
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
            CREATE TABLE IF NOT EXISTS st_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'gift',
                status TEXT NOT NULL DEFAULT 'signups',
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                announce_channel_id INTEGER,
                announce_message_id INTEGER,
                signups_close_at TEXT NOT NULL,
                exchange_by TEXT NOT NULL,
                budget TEXT,
                rules TEXT,
                public_page INTEGER NOT NULL DEFAULT 1,
                drawn_at TEXT, revealed_at TEXT, closed_at TEXT,
                reminded_7 INTEGER NOT NULL DEFAULT 0,
                reminded_2 INTEGER NOT NULL DEFAULT 0,
                web_dirty INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS st_entries (
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                neopets_name TEXT NOT NULL,
                wants TEXT NOT NULL DEFAULT '',
                links TEXT NOT NULL DEFAULT '',
                avoid TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'discord',
                joined_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (event_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS st_blocks (
                event_id INTEGER NOT NULL,
                user_a INTEGER NOT NULL,
                user_b INTEGER NOT NULL,
                PRIMARY KEY (event_id, user_a, user_b)
            );
            CREATE TABLE IF NOT EXISTS st_matches (
                event_id INTEGER NOT NULL,
                giver_id INTEGER NOT NULL,
                giftee_id INTEGER NOT NULL,
                sent_at TEXT,
                received_at TEXT,
                thanks TEXT,
                PRIMARY KEY (event_id, giver_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_st_matches_giftee ON st_matches(event_id, giftee_id);
            CREATE TABLE IF NOT EXISTS st_lookups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                mod_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                at TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(st_events)").fetchall()}
        if "opens_at" not in columns:
            conn.execute("ALTER TABLE st_events ADD COLUMN opens_at TEXT")
        if "ping_role_id" not in columns:
            conn.execute("ALTER TABLE st_events ADD COLUMN ping_role_id INTEGER")
        if "closed_notice" not in columns:
            conn.execute("ALTER TABLE st_events ADD COLUMN closed_notice INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE st_events SET closed_notice = 1")      # exchanges that already existed don't need a notice
        conn.commit()


# ---------------------------------------------------------------- events

def get_event(event_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM st_events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def active_events(include_scheduled=False):
    """Every exchange that's currently running, oldest first. Several can run at once.
    include_scheduled also lists exchanges that are waiting for their opening day."""
    states = "('signups', 'drawn', 'revealed', 'scheduled')" if include_scheduled else "('signups', 'drawn', 'revealed')"
    with _db() as conn:
        rows = conn.execute(f"SELECT * FROM st_events WHERE status IN {states} ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def active_event():
    """The most recently started running exchange, or None."""
    events = active_events()
    return events[-1] if events else None


def event_by_message(message_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM st_events WHERE announce_message_id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def create_event(name, kind, created_by, channel_id, signups_close_at, exchange_by, budget, rules, public_page, now,
                 opens_at=None, ping_role_id=None):
    """Returns (True, event_id) or (False, reason). With a future opens_at the exchange waits ('scheduled')
    and the bot opens it by itself on that day."""
    scheduled = opens_at is not None and opens_at > now + timedelta(minutes=2)
    with _txn() as conn:
        if conn.execute("SELECT 1 FROM st_events WHERE status IN ('signups', 'drawn', 'revealed', 'scheduled') AND LOWER(name) = LOWER(?)",
                        (name,)).fetchone():
            return (False, "A running exchange already has that name. Pick a different name so people can tell them apart.")
        cur = conn.execute(
            "INSERT INTO st_events (name, kind, status, created_by, created_at, announce_channel_id, signups_close_at, "
            "exchange_by, budget, rules, public_page, opens_at, ping_role_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, kind if kind in KINDS else "other", "scheduled" if scheduled else "signups", created_by, _iso(now), channel_id,
             _iso(signups_close_at), _iso(exchange_by), budget, rules, 1 if public_page else 0,
             _iso(opens_at) if scheduled else None, ping_role_id))
        return (True, cur.lastrowid)


def set_announcement(event_id, channel_id, message_id):
    with _db() as conn:
        conn.execute("UPDATE st_events SET announce_channel_id = ?, announce_message_id = ? WHERE id = ?",
                     (channel_id, message_id, event_id))
        conn.commit()


def mark_dirty(event_id):
    """The website and the announcement's counts are out of date; the background loop refreshes them."""
    with _db() as conn:
        conn.execute("UPDATE st_events SET web_dirty = 1 WHERE id = ?", (event_id,))
        conn.commit()


# ---------------------------------------------------------------- entries

NP_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
URL_RE = re.compile(r"^https?://\S{3,300}$", re.IGNORECASE)


def validate_entry(neopets_name, wants, links, avoid):
    """Checks and tidies what a member typed (in Discord or on the website).
    Returns (True, cleaned_dict) or (False, message)."""
    name = (neopets_name or "").strip()
    if not NP_NAME_RE.match(name):
        return (False, "Your Neopets username should be 3 to 20 letters, numbers or underscores.")
    wants = (wants or "").strip()
    if not wants:
        return (False, "Please tell your Santa a little about what you'd like.")
    if len(wants) > WANTS_MAX:
        return (False, f"That's a bit long: please keep it under {WANTS_MAX} characters (yours is {len(wants)}).")
    link_lines = [ln.strip() for ln in (links or "").replace("\r", "").split("\n") if ln.strip()]
    if len(link_lines) > LINKS_MAX_COUNT:
        return (False, f"Please share at most {LINKS_MAX_COUNT} links.")
    for n, ln in enumerate(link_lines, start=1):
        if not URL_RE.match(ln):
            return (False, f"Link {n} needs to start with http:// or https:// (one link per line).")
    avoid = (avoid or "").strip()
    if len(avoid) > AVOID_MAX:
        return (False, f"The \"please avoid\" note is too long: keep it under {AVOID_MAX} characters.")
    return (True, {"neopets_name": name, "wants": wants, "links": "\n".join(link_lines), "avoid": avoid})


def entries_for(event_id):
    with _db() as conn:
        rows = conn.execute("SELECT * FROM st_entries WHERE event_id = ? ORDER BY joined_at, user_id", (event_id,)).fetchall()
    return [dict(r) for r in rows]


def get_entry(event_id, user_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM st_entries WHERE event_id = ? AND user_id = ?", (event_id, user_id)).fetchone()
    return dict(row) if row else None


def upsert_entry(event_id, user_id, display_name, cleaned, source, now):
    """Adds or updates someone's entry. New people can only join while sign-ups are open; people already
    in can keep editing their wishlist after the draw. Returns (True, 'created' | 'updated') or (False, reason)."""
    with _txn() as conn:
        ev = conn.execute("SELECT * FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] not in ("signups", "drawn", "revealed"):
            return (False, "There's no exchange taking sign-ups right now.")
        existing = conn.execute("SELECT 1 FROM st_entries WHERE event_id = ? AND user_id = ?", (event_id, user_id)).fetchone()
        if not existing:
            if ev["status"] != "signups":
                return (False, "Sign-ups are closed: the matches have already been drawn.")
            if now >= _parse(ev["signups_close_at"]):
                return (False, "Sign-ups have closed.")
            n = conn.execute("SELECT COUNT(*) AS n FROM st_entries WHERE event_id = ?", (event_id,)).fetchone()["n"]
            if n >= MAX_PLAYERS:
                return (False, f"This exchange is full ({MAX_PLAYERS} people).")
            conn.execute(
                "INSERT INTO st_entries (event_id, user_id, display_name, neopets_name, wants, links, avoid, source, joined_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, user_id, display_name, cleaned["neopets_name"], cleaned["wants"], cleaned["links"], cleaned["avoid"],
                 source, _iso(now), _iso(now)))
            conn.execute("UPDATE st_events SET web_dirty = 1 WHERE id = ?", (event_id,))
            return (True, "created")
        conn.execute(
            "UPDATE st_entries SET display_name = ?, neopets_name = ?, wants = ?, links = ?, avoid = ?, updated_at = ? "
            "WHERE event_id = ? AND user_id = ?",
            (display_name, cleaned["neopets_name"], cleaned["wants"], cleaned["links"], cleaned["avoid"], _iso(now), event_id, user_id))
        return (True, "updated")


def leave_event(event_id, user_id):
    """Someone withdraws before the draw."""
    with _txn() as conn:
        ev = conn.execute("SELECT status FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] != "signups":
            return (False, "The matches have already been drawn, so ask a mod to remove you.")
        cur = conn.execute("DELETE FROM st_entries WHERE event_id = ? AND user_id = ?", (event_id, user_id))
        if cur.rowcount == 0:
            return (False, "You're not signed up.")
        conn.execute("UPDATE st_events SET web_dirty = 1 WHERE id = ?", (event_id,))
        return (True, "ok")


# ---------------------------------------------------------------- blocked pairs

def _pair(a, b):
    return (a, b) if a < b else (b, a)


def add_block(event_id, a, b):
    if a == b:
        return (False, "Pick two different members.")
    with _txn() as conn:
        ev = conn.execute("SELECT status FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] != "signups":
            return (False, "Pairs can only be blocked before the draw.")
        x, y = _pair(a, b)
        conn.execute("INSERT OR IGNORE INTO st_blocks (event_id, user_a, user_b) VALUES (?, ?, ?)", (event_id, x, y))
        return (True, "ok")


def remove_block(event_id, a, b):
    with _txn() as conn:
        x, y = _pair(a, b)
        cur = conn.execute("DELETE FROM st_blocks WHERE event_id = ? AND user_a = ? AND user_b = ?", (event_id, x, y))
        return (True, "ok") if cur.rowcount else (False, "Those two weren't blocked.")


def blocked_pairs(event_id):
    with _db() as conn:
        rows = conn.execute("SELECT user_a, user_b FROM st_blocks WHERE event_id = ?", (event_id,)).fetchall()
    return {frozenset((r["user_a"], r["user_b"])) for r in rows}


# ============================================================
#  THE DRAW
# ============================================================

class DrawImpossible(Exception):
    pass


def _allowed(a, b, blocked):
    return a != b and frozenset((a, b)) not in blocked


def _search(ids, blocked, rng, mutual_ok, node_limit=200000):
    """Backtracking search for a valid draw (used only when a simple shuffle can't satisfy the blocked pairs)."""
    order = sorted(ids, key=lambda x: (sum(1 for y in ids if not _allowed(x, y, blocked)), rng.random()), reverse=True)
    assignment, taken, nodes = {}, set(), [0]

    def rec(i):
        if i == len(order):
            return True
        nodes[0] += 1
        if nodes[0] > node_limit:
            return False
        giver = order[i]
        options = [t for t in ids if t not in taken and _allowed(giver, t, blocked) and (mutual_ok or assignment.get(t) != giver)]
        rng.shuffle(options)
        for target in options:
            assignment[giver] = target
            taken.add(target)
            if rec(i + 1):
                return True
            del assignment[giver]
            taken.discard(target)
        return False

    return dict(assignment) if rec(0) else None


def compute_matches(ids, blocked, rng=None, avoid_mutual=True):
    """Works out who gives to whom. Returns {giver_id: giftee_id}.
    Rules: nobody gets themselves; blocked pairs are never matched (in either direction); and, whenever it's
    possible, nobody ends up swapping with the person who has them (A->B and B->A), which would give the
    surprise away. Raises DrawImpossible if the rules can't all be met."""
    rng = rng or random.Random()
    ids = list(ids)
    n = len(ids)
    if n < MIN_PLAYERS:
        raise DrawImpossible(f"An exchange needs at least {MIN_PLAYERS} people.")
    # 1) one big circle: everyone gives to the next person. It can never contain a swap.
    for _ in range(400):
        order = ids[:]
        rng.shuffle(order)
        if all(_allowed(order[i], order[(i + 1) % n], blocked) for i in range(n)):
            return {order[i]: order[(i + 1) % n] for i in range(n)}
    # 2) the blocked pairs make a circle hard, so search more carefully
    for mutual_ok in ((False, True) if avoid_mutual else (True,)):
        result = _search(ids, blocked, rng, mutual_ok)
        if result:
            return result
    raise DrawImpossible("There's no way to match everyone with those blocked pairs. Remove a block and try again.")


def draw_event(event_id, now, rng=None):
    """Draws the matches. Returns (True, info) or (False, reason)."""
    with _txn() as conn:
        ev = conn.execute("SELECT * FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] != "signups":
            return (False, "This exchange isn't taking sign-ups, so it can't be drawn.")
        ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM st_entries WHERE event_id = ?", (event_id,)).fetchall()]
        if len(ids) < MIN_PLAYERS:
            return (False, f"Only {len(ids)} signed up. An exchange needs at least {MIN_PLAYERS}.")
        blocks = {frozenset((r["user_a"], r["user_b"])) for r in
                  conn.execute("SELECT user_a, user_b FROM st_blocks WHERE event_id = ?", (event_id,)).fetchall()}
        try:
            matches = compute_matches(ids, blocks, rng)
        except DrawImpossible as e:
            return (False, str(e))
        for giver, giftee in matches.items():
            conn.execute("INSERT INTO st_matches (event_id, giver_id, giftee_id) VALUES (?, ?, ?)", (event_id, giver, giftee))
        conn.execute("UPDATE st_events SET status = 'drawn', drawn_at = ?, web_dirty = 1 WHERE id = ?", (_iso(now), event_id))
        return (True, {"players": len(ids)})


# ============================================================
#  AFTER THE DRAW
# ============================================================

def giftee_of(event_id, user_id):
    """The person this member gives to (their entry), or None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT e.* FROM st_matches m JOIN st_entries e ON e.event_id = m.event_id AND e.user_id = m.giftee_id "
            "WHERE m.event_id = ? AND m.giver_id = ?", (event_id, user_id)).fetchone()
    return dict(row) if row else None


def match_row_as_giver(event_id, user_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM st_matches WHERE event_id = ? AND giver_id = ?", (event_id, user_id)).fetchone()
    return dict(row) if row else None


def match_row_as_giftee(event_id, user_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM st_matches WHERE event_id = ? AND giftee_id = ?", (event_id, user_id)).fetchone()
    return dict(row) if row else None


def mark_sent(event_id, user_id, now, undo=False):
    with _txn() as conn:
        ev = conn.execute("SELECT status FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] not in ("drawn", "revealed"):
            return (False, "The matches haven't been drawn yet.")
        row = conn.execute("SELECT * FROM st_matches WHERE event_id = ? AND giver_id = ?", (event_id, user_id)).fetchone()
        if not row:
            return (False, "You're not part of this exchange.")
        if undo:
            conn.execute("UPDATE st_matches SET sent_at = NULL WHERE event_id = ? AND giver_id = ?", (event_id, user_id))
            conn.execute("UPDATE st_events SET web_dirty = 1 WHERE id = ?", (event_id,))
            return (True, "unmarked")
        if row["sent_at"]:
            return (True, "already")
        conn.execute("UPDATE st_matches SET sent_at = ? WHERE event_id = ? AND giver_id = ?", (_iso(now), event_id, user_id))
        conn.execute("UPDATE st_events SET web_dirty = 1 WHERE id = ?", (event_id,))
        return (True, "marked")


def mark_received(event_id, user_id, now, thanks=None):
    """The receiver confirms their gift arrived; an optional thank-you note is shown at the reveal."""
    with _txn() as conn:
        ev = conn.execute("SELECT status FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] not in ("drawn", "revealed"):
            return (False, "The matches haven't been drawn yet.")
        row = conn.execute("SELECT * FROM st_matches WHERE event_id = ? AND giftee_id = ?", (event_id, user_id)).fetchone()
        if not row:
            return (False, "You're not part of this exchange.")
        thanks = (thanks or "").strip()[:THANKS_MAX] or None
        already = row["received_at"] is not None
        conn.execute("UPDATE st_matches SET received_at = COALESCE(received_at, ?), thanks = COALESCE(?, thanks) "
                     "WHERE event_id = ? AND giftee_id = ?", (_iso(now), thanks, event_id, user_id))
        conn.execute("UPDATE st_events SET web_dirty = 1 WHERE id = ?", (event_id,))
        return (True, "already" if already else "marked")


def counts(event_id):
    with _db() as conn:
        entries = conn.execute("SELECT COUNT(*) AS n FROM st_entries WHERE event_id = ?", (event_id,)).fetchone()["n"]
        row = conn.execute("SELECT COUNT(*) AS matched, COUNT(sent_at) AS sent, COUNT(received_at) AS received "
                           "FROM st_matches WHERE event_id = ?", (event_id,)).fetchone()
    return {"signed_up": entries, "matched": row["matched"], "sent": row["sent"], "received": row["received"]}


def givers_not_sent(event_id):
    with _db() as conn:
        rows = conn.execute(
            "SELECT m.giver_id, e.display_name FROM st_matches m JOIN st_entries e ON e.event_id = m.event_id AND e.user_id = m.giver_id "
            "WHERE m.event_id = ? AND m.sent_at IS NULL ORDER BY e.display_name", (event_id,)).fetchall()
    return [dict(r) for r in rows]


def lookup_match(event_id, mod_id, target_id, now):
    """An emergency look-up by a mod: who has this member, and who they have. Always recorded."""
    with _txn() as conn:
        ev = conn.execute("SELECT status FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] not in ("drawn", "revealed"):
            return (False, "The matches haven't been drawn yet.")
        if not conn.execute("SELECT 1 FROM st_entries WHERE event_id = ? AND user_id = ?", (event_id, target_id)).fetchone():
            return (False, "That member isn't in this exchange.")
        conn.execute("INSERT INTO st_lookups (event_id, mod_id, target_id, at) VALUES (?, ?, ?, ?)",
                     (event_id, mod_id, target_id, _iso(now)))
        santa = conn.execute("SELECT e.display_name FROM st_matches m JOIN st_entries e ON e.event_id = m.event_id AND e.user_id = m.giver_id "
                             "WHERE m.event_id = ? AND m.giftee_id = ?", (event_id, target_id)).fetchone()
        giftee = conn.execute("SELECT e.display_name FROM st_matches m JOIN st_entries e ON e.event_id = m.event_id AND e.user_id = m.giftee_id "
                              "WHERE m.event_id = ? AND m.giver_id = ?", (event_id, target_id)).fetchone()
        n = conn.execute("SELECT COUNT(*) AS n FROM st_lookups WHERE event_id = ?", (event_id,)).fetchone()["n"]
        return (True, {"santa": santa["display_name"] if santa else None,
                       "giftee": giftee["display_name"] if giftee else None, "lookup_number": n})


def drop_participant(event_id, user_id):
    """Removes someone, and after the draw repairs the matches. If S gave to the person leaving (D) and D gave
    to G, then S now gives to G. If that isn't allowed, one other pair is swapped to make it work.
    Returns a dict describing what happened, or (False, reason)."""
    with _txn() as conn:
        ev = conn.execute("SELECT * FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] not in ("signups", "drawn"):
            return (False, "People can only be removed before the reveal.")
        entry = conn.execute("SELECT * FROM st_entries WHERE event_id = ? AND user_id = ?", (event_id, user_id)).fetchone()
        if not entry:
            return (False, "That member isn't in this exchange.")
        conn.execute("UPDATE st_events SET web_dirty = 1 WHERE id = ?", (event_id,))
        if ev["status"] == "signups":
            conn.execute("DELETE FROM st_entries WHERE event_id = ? AND user_id = ?", (event_id, user_id))
            return {"phase": "signups", "repaired": True, "notify": []}

        blocks = {frozenset((r["user_a"], r["user_b"])) for r in
                  conn.execute("SELECT user_a, user_b FROM st_blocks WHERE event_id = ?", (event_id,)).fetchall()}
        m_in = conn.execute("SELECT * FROM st_matches WHERE event_id = ? AND giftee_id = ?", (event_id, user_id)).fetchone()   # S -> D
        m_out = conn.execute("SELECT * FROM st_matches WHERE event_id = ? AND giver_id = ?", (event_id, user_id)).fetchone()   # D -> G
        conn.execute("DELETE FROM st_matches WHERE event_id = ? AND (giver_id = ? OR giftee_id = ?)", (event_id, user_id, user_id))
        conn.execute("DELETE FROM st_entries WHERE event_id = ? AND user_id = ?", (event_id, user_id))
        if not m_in or not m_out:
            return {"phase": "drawn", "repaired": True, "notify": []}
        S, G = m_in["giver_id"], m_out["giftee_id"]
        had_sent = m_in["sent_at"] is not None
        if S == G:
            return {"phase": "drawn", "repaired": False, "orphan": S, "reason": "the two of them were only matched with each other"}
        if _allowed(S, G, blocks):
            conn.execute("INSERT INTO st_matches (event_id, giver_id, giftee_id) VALUES (?, ?, ?)", (event_id, S, G))
            return {"phase": "drawn", "repaired": True, "notify": [S], "had_sent": had_sent}
        # S can't give to G: look for another pair (X -> Y) to swap with, so that S -> Y and X -> G are both allowed
        others = conn.execute("SELECT * FROM st_matches WHERE event_id = ?", (event_id,)).fetchall()
        for pair in others:
            X, Y = pair["giver_id"], pair["giftee_id"]
            if X in (S, G) or Y in (S, G):
                continue
            if _allowed(S, Y, blocks) and _allowed(X, G, blocks):
                conn.execute("DELETE FROM st_matches WHERE event_id = ? AND giver_id = ?", (event_id, X))
                conn.execute("INSERT INTO st_matches (event_id, giver_id, giftee_id) VALUES (?, ?, ?)", (event_id, S, Y))
                conn.execute("INSERT INTO st_matches (event_id, giver_id, giftee_id) VALUES (?, ?, ?)", (event_id, X, G))
                return {"phase": "drawn", "repaired": True, "notify": [S, X], "had_sent": had_sent, "swapped": True}
        return {"phase": "drawn", "repaired": False, "stuck": (S, G), "reason": "a blocked pair stops them being matched and no swap fixes it"}


def reveal_event(event_id, now):
    with _txn() as conn:
        ev = conn.execute("SELECT status FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] != "drawn":
            return (False, "Only an exchange that has been drawn can be revealed.")
        conn.execute("UPDATE st_events SET status = 'revealed', revealed_at = ?, web_dirty = 1 WHERE id = ?", (_iso(now), event_id))
        return (True, "ok")


def close_event(event_id, now):
    """Ends the exchange and erases everyone's wishlists, links and notes."""
    with _txn() as conn:
        ev = conn.execute("SELECT status FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] not in ("signups", "drawn", "revealed"):
            return (False, "That exchange has already ended.")
        conn.execute("UPDATE st_events SET status = 'closed', closed_at = ?, web_dirty = 1 WHERE id = ?", (_iso(now), event_id))
        conn.execute("UPDATE st_entries SET wants = '', links = '', avoid = '' WHERE event_id = ?", (event_id,))
        return (True, "ok")


def cancel_event(event_id, now):
    with _txn() as conn:
        ev = conn.execute("SELECT status FROM st_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or ev["status"] not in ("signups", "scheduled"):
            return (False, "Only an exchange that hasn't been drawn can be cancelled. Use /santa close for the rest.")
        conn.execute("UPDATE st_events SET status = 'cancelled', closed_at = ?, web_dirty = 1 WHERE id = ?", (_iso(now), event_id))
        conn.execute("UPDATE st_entries SET wants = '', links = '', avoid = '' WHERE event_id = ?", (event_id,))
        return (True, "ok")


def reveal_pairs(event_id):
    """Who had whom, with any thank-you note. Only used for the reveal."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT g.display_name AS giver_name, g.neopets_name AS giver_np, r.display_name AS giftee_name, "
            "r.neopets_name AS giftee_np, m.thanks AS note FROM st_matches m "
            "JOIN st_entries g ON g.event_id = m.event_id AND g.user_id = m.giver_id "
            "JOIN st_entries r ON r.event_id = m.event_id AND r.user_id = m.giftee_id "
            "WHERE m.event_id = ? ORDER BY g.display_name", (event_id,)).fetchall()
    return [dict(r) for r in rows]


# ============================================================
#  SMALL DISCORD HELPERS
# ============================================================

def _is_mod(user):
    return any(r.name in _mod_roles for r in getattr(user, "roles", []))


async def _deny_wrong_guild(interaction):
    if interaction.guild is None or interaction.guild.name not in _guild_names:
        await interaction.response.send_message("⚠️ Secret Santa isn't active in this server.", ephemeral=True)
        return True
    return False


async def _deny_non_mod(interaction):
    if not _is_mod(interaction.user):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return True
    return False


def web_enabled():
    return bool(WEB_SYNC_URL) and bool(os.environ.get(WEB_SECRET_ENV))


def web_link(event):
    if not web_enabled() or not event.get("public_page"):
        return None
    return WEB_PUBLIC_URL.rstrip("/") + "/?e=" + str(event["id"])


async def _get_channel(channel_id):
    if not channel_id:
        return None
    channel = _bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await _bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    return channel


async def _dm(user_id, content):
    """Best-effort private message. Returns True if it was delivered."""
    try:
        user = _bot.get_user(user_id) or await _bot.fetch_user(user_id)
        await user.send(content)
        return True
    except Exception:
        return False


async def _mod_log(text):
    """Writes to the mod channel. Returns True if it was posted."""
    try:
        guild = discord.utils.get(_bot.guilds, name=_guild_names[0])
        channel = discord.utils.get(guild.text_channels, name=_mod_channel_name) if guild is not None and _mod_channel_name else None
        if channel is None:
            print("⚠️ Santa: couldn't find the mod channel to log to")
            return False
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        return True
    except Exception as e:
        print(f"⚠️ Santa: couldn't write to the mod channel ({type(e).__name__})")
        return False


def _kind(event):
    return KINDS.get(event["kind"], KINDS["other"])


# ============================================================
#  THE ANNOUNCEMENT POST AND ITS BUTTONS
# ============================================================

def announcement_embed(event):
    kind = _kind(event)
    c = counts(event["id"])
    status = event["status"]
    embed = discord.Embed(title=f"🎅 {event['name']}", color=EMBED_COLOR)
    lines = []
    if status == "signups" and _now() >= _parse(event["signups_close_at"]):
        lines.append("🔒 **Sign-ups are closed.** The matches will be drawn soon!")
    elif status == "signups":
        lines.append(f"Join our **{kind['label'].lower()}**! You'll be matched with one person to give a {kind['noun']} to, "
                     f"and one other person will be giving to you. Nobody finds out who until the reveal.")
        lines.append(f"**Sign-ups close:** {_ts(event['signups_close_at'], 'F')} ({_ts(event['signups_close_at'], 'R')})")
    elif status == "drawn":
        lines.append("🎉 **The matches are out!** Press **🎅 My giftee** to see who you're giving to. Only you can see it.")
    elif status == "revealed":
        lines.append("🎊 **The reveal is here!** Thank you to everyone who took part.")
    else:
        lines.append("This exchange has ended. Thank you to everyone who took part! 💝")
    if status in ("signups", "drawn"):
        lines.append(f"**{kind['noun'].capitalize()}s due by:** {_ts(event['exchange_by'], 'F')} ({_ts(event['exchange_by'], 'R')})")
    if event.get("budget"):
        lines.append(f"**Guidelines:** {event['budget']}")
    if event.get("rules"):
        lines.append(f"**Rules:** {event['rules']}")
    link = web_link(event)
    if link:
        lines.append("🌐 Event page" + (" and website sign-up" if status == "signups" else "") + f": {link}")
    embed.description = "\n\n".join(lines)[:4000]
    if status == "signups":
        embed.add_field(name="Signed up", value=str(c["signed_up"]), inline=True)
    elif status in ("drawn", "revealed"):
        embed.add_field(name="Players", value=str(c["matched"]), inline=True)
        embed.add_field(name="Sent", value=f"{c['sent']} / {c['matched']}", inline=True)
        embed.add_field(name="Received", value=f"{c['received']} / {c['matched']}", inline=True)
    embed.set_footer(text="Your match is secret: only you can see it.")
    return embed


async def post_announcement(event, channel):
    """Posts the announcement (with its buttons). Pings the chosen role, if there is one. Raises if it can't post."""
    role_id = event.get("ping_role_id")
    text = (f"<@&{role_id}> " if role_id else "") + f"🎄 **{event['name']}**: sign-ups are open until {_ts(event['signups_close_at'], 'F')}!"
    mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=role_id)] if role_id else False)
    return await channel.send(content=text, embed=announcement_embed(event), view=SantaView(), allowed_mentions=mentions)


async def refresh_announcement(event_id):
    event = get_event(event_id)
    if not event or not event["announce_message_id"]:
        return
    try:
        channel = await _get_channel(event["announce_channel_id"])
        message = await channel.fetch_message(event["announce_message_id"])
        await message.edit(embed=announcement_embed(event), view=SantaView())
    except Exception as e:
        print(f"⚠️ Santa: couldn't refresh the announcement ({type(e).__name__})")


class SantaView(discord.ui.View):
    """The buttons under the announcement. They keep working after restarts because the exchange is looked
    up from the message the button is on."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _event(self, interaction):
        event = event_by_message(interaction.message.id) if interaction.message else None
        if event is None:
            await interaction.response.send_message("⚠️ This exchange has ended.", ephemeral=True)
        return event

    @discord.ui.button(label="Join / edit my entry", style=discord.ButtonStyle.success, emoji="🎁", custom_id="st_join", row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        event = await self._event(interaction)
        if event:
            await open_entry_form(interaction, event)

    @discord.ui.button(label="My giftee", style=discord.ButtonStyle.primary, emoji="🎅", custom_id="st_match", row=0)
    async def match(self, interaction: discord.Interaction, button: discord.ui.Button):
        event = await self._event(interaction)
        if event:
            await show_match(interaction, event)

    @discord.ui.button(label="I sent my gift", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="st_sent", row=1)
    async def sent(self, interaction: discord.Interaction, button: discord.ui.Button):
        event = await self._event(interaction)
        if event:
            await do_mark_sent(interaction, event)

    @discord.ui.button(label="I got my gift", style=discord.ButtonStyle.secondary, emoji="💌", custom_id="st_received", row=1)
    async def received(self, interaction: discord.Interaction, button: discord.ui.Button):
        event = await self._event(interaction)
        if event:
            await open_received_form(interaction, event)


# ---------------------------------------------------------------- the sign-up form

class EntryModal(discord.ui.Modal):
    def __init__(self, event, existing=None):
        super().__init__(title=f"Entry: {event['name']}"[:45])
        self.event_id = event["id"]
        kind = _kind(event)
        self.neopets = discord.ui.TextInput(label="Your Neopets username", required=True, min_length=3, max_length=20,
                                            default=(existing or {}).get("neopets_name") or None)
        self.wants = discord.ui.TextInput(label=kind["wants_label"][:45], style=discord.TextStyle.paragraph, required=True,
                                          max_length=WANTS_MAX, placeholder=kind["wants_hint"][:100],
                                          default=(existing or {}).get("wants") or None)
        self.links = discord.ui.TextInput(label=kind["links_label"][:45], style=discord.TextStyle.paragraph, required=False,
                                          max_length=LINKS_MAX_COUNT * 120, placeholder="https://...",
                                          default=(existing or {}).get("links") or None)
        self.avoid = discord.ui.TextInput(label=kind["avoid_label"][:45], style=discord.TextStyle.paragraph, required=False,
                                          max_length=AVOID_MAX, default=(existing or {}).get("avoid") or None)
        for item in (self.neopets, self.wants, self.links, self.avoid):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        ok, cleaned = validate_entry(self.neopets.value, self.wants.value, self.links.value, self.avoid.value)
        if not ok:
            await interaction.response.send_message(f"⚠️ {cleaned}\nPress the 🎁 button to try again. Nothing was saved.", ephemeral=True)
            return
        ok, result = upsert_entry(self.event_id, interaction.user.id, interaction.user.display_name, cleaned, "discord", _now())
        if not ok:
            await interaction.response.send_message(f"⚠️ {result}", ephemeral=True)
            return
        await interaction.response.send_message(_entry_saved_text(result), ephemeral=True)


def _entry_saved_text(result):
    if result == "created":
        return ("🎁 **You're in!** Your Santa will see this when matches are drawn. "
                "You can change it any time with the 🎁 button or `/santa join`.")
    return "✅ Your entry was updated."


async def open_entry_form(interaction, event):
    if await _deny_wrong_guild(interaction):
        return
    if getattr(interaction.user, "bot", False):
        await interaction.response.send_message("⚠️ Bots can't take part.", ephemeral=True)
        return
    existing = get_entry(event["id"], interaction.user.id)
    if event["status"] in ("closed", "cancelled") or (event["status"] == "revealed" and not existing):
        await interaction.response.send_message("⚠️ This exchange isn't taking sign-ups." if event["status"] != "closed"
                                                else "This exchange has ended. Thank you for taking part! 💝", ephemeral=True)
        return
    if not existing:
        if event["status"] != "signups":
            await interaction.response.send_message("⚠️ Sign-ups are closed: the matches have already been drawn.", ephemeral=True)
            return
        if _now() >= _parse(event["signups_close_at"]):
            await interaction.response.send_message("⚠️ Sign-ups have closed.", ephemeral=True)
            return
    await interaction.response.send_modal(EntryModal(event, existing))


# ---------------------------------------------------------------- my giftee

class SentView(discord.ui.View):
    def __init__(self, event_id, user_id):
        super().__init__(timeout=900)
        self.event_id, self.user_id = event_id, user_id

    @discord.ui.button(label="I sent my gift", style=discord.ButtonStyle.success, emoji="📦")
    async def sent(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⚠️ This belongs to someone else.", ephemeral=True)
            return
        ok, result = mark_sent(self.event_id, interaction.user.id, _now())
        await interaction.response.send_message(_sent_text(ok, result), ephemeral=True)


def _sent_text(ok, result):
    if not ok:
        return f"⚠️ {result}"
    return {"marked": "📦 Marked as sent. Thank you!", "already": "You'd already marked your gift as sent. Thank you!",
            "unmarked": "Okay, your gift is marked as not sent yet."}[result]


def _match_embed(event, giftee, giver_row):
    kind = _kind(event)
    embed = discord.Embed(title=f"🎅 You're the Secret Santa for {giftee['display_name']}!", color=EMBED_COLOR)
    embed.description = f"**{event['name']}**\nTheir Neopets username: **{giftee['neopets_name']}**"
    embed.add_field(name=kind["wants_label"][:256], value=(giftee["wants"] or "—")[:1024], inline=False)
    if giftee["links"]:
        embed.add_field(name="Links", value=giftee["links"][:1024], inline=False)
    if giftee["avoid"]:
        embed.add_field(name="Please avoid", value=giftee["avoid"][:1024], inline=False)
    embed.add_field(name="Due by", value=_ts(event["exchange_by"], "F"), inline=False)
    if event.get("budget"):
        embed.add_field(name="Guidelines", value=event["budget"][:1024], inline=False)
    state = "📦 You've marked your gift as sent." if giver_row["sent_at"] else "You haven't marked your gift as sent yet."
    embed.set_footer(text=f"{state} Keep it secret! 🤫")
    return embed


async def show_match(interaction, event):
    if await _deny_wrong_guild(interaction):
        return
    user = interaction.user
    if event["status"] == "signups":
        await interaction.response.send_message("🎅 The matches haven't been drawn yet. Hang tight!", ephemeral=True)
        return
    if event["status"] in ("closed", "cancelled"):
        await interaction.response.send_message("This exchange has ended. Thank you for taking part! 💝", ephemeral=True)
        return
    row = match_row_as_giver(event["id"], user.id)
    giftee = giftee_of(event["id"], user.id)
    if not row or not giftee:
        mine = get_entry(event["id"], user.id)
        await interaction.response.send_message(
            "⚠️ You're signed up, but you don't have a match." if mine else "⚠️ You're not part of this exchange.", ephemeral=True)
        return
    await interaction.response.send_message(embed=_match_embed(event, giftee, row), view=SentView(event["id"], user.id), ephemeral=True)


async def do_mark_sent(interaction, event, undo=False):
    if await _deny_wrong_guild(interaction):
        return
    ok, result = mark_sent(event["id"], interaction.user.id, _now(), undo=undo)
    await interaction.response.send_message(f"**{event['name']}**: " + _sent_text(ok, result), ephemeral=True)


class ReceivedModal(discord.ui.Modal):
    def __init__(self, event_id):
        super().__init__(title="Your gift arrived!")
        self.event_id = event_id
        self.thanks = discord.ui.TextInput(label="Thank-you note (shown at reveal; optional)", style=discord.TextStyle.paragraph,
                                           required=False, max_length=THANKS_MAX, placeholder="Say thanks to your Santa!")
        self.add_item(self.thanks)

    async def on_submit(self, interaction: discord.Interaction):
        ok, result = mark_received(self.event_id, interaction.user.id, _now(), self.thanks.value)
        if not ok:
            await interaction.response.send_message(f"⚠️ {result}", ephemeral=True)
            return
        await interaction.response.send_message(
            "💌 Marked as received. Your Santa will be so happy!" if result == "marked" else "✅ Updated your thank-you note.", ephemeral=True)


async def open_received_form(interaction, event):
    if await _deny_wrong_guild(interaction):
        return
    if event["status"] not in ("drawn", "revealed"):
        await interaction.response.send_message("⚠️ The matches haven't been drawn yet." if event["status"] == "signups"
                                                else "This exchange has ended.", ephemeral=True)
        return
    if not match_row_as_giftee(event["id"], interaction.user.id):
        await interaction.response.send_message("⚠️ You're not part of this exchange.", ephemeral=True)
        return
    await interaction.response.send_modal(ReceivedModal(event["id"]))


# ============================================================
#  THE WEBSITE: public page, and sign-ups made there
# ============================================================

def build_web_payload(event):
    """Exactly what's sent to the website: event info and COUNTS. Wishlists, links and matches are never
    sent. Only after a mod runs /santa reveal are the pairs (and thank-you notes) included."""
    payload = {
        "event": {
            "id": event["id"], "name": event["name"], "kind": event["kind"], "status": event["status"],
            "signups_close_at": _parse(event["signups_close_at"]).isoformat(),
            "exchange_by": _parse(event["exchange_by"]).isoformat(),
            "budget": event["budget"], "rules": event["rules"],
            "counts": counts(event["id"]),
        },
        "reveal": [],
    }
    if event["status"] in ("revealed", "closed"):
        payload["reveal"] = reveal_pairs(event["id"])
    return payload


async def push_web(event_id):
    """Sends the event to the website. Never raises: a website problem must never get in the way of an exchange."""
    if not web_enabled():
        return False
    event = get_event(event_id)
    if not event or not event["public_page"]:
        return False
    try:
        payload = build_web_payload(event)
        async with aiohttp.ClientSession() as session:
            async with session.post(WEB_SYNC_URL, json=payload, headers={"X-Sync-Secret": os.environ.get(WEB_SECRET_ENV, "")},
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    result = {}
                ok = resp.status == 200 and isinstance(result, dict) and bool(result.get("success"))
                problem = (result.get("error") if isinstance(result, dict) else None) or f"HTTP {resp.status}"
    except Exception as e:
        print(f"⚠️ Santa: couldn't reach the website ({type(e).__name__})")
        return False
    if not ok:
        print(f"⚠️ Santa: the website rejected the update ({problem})")
        return False
    _last_web_push[event_id] = time.monotonic()
    with _db() as conn:
        conn.execute("UPDATE st_events SET web_dirty = 0 WHERE id = ?", (event_id,))
        conn.commit()
    return True


def _normalize_code(code):
    return re.sub(r"[^A-Za-z0-9]", "", code or "").upper()


async def claim_web_signup(code):
    """Collects a website sign-up using its code. The website deletes its copy as soon as it's collected.
    Returns (True, entry_dict) or (False, message)."""
    if not web_enabled():
        return (False, "Website sign-ups aren't set up.")
    code = _normalize_code(code)
    if len(code) != 8:
        return (False, "That code doesn't look right. It's 8 letters and numbers, like `K7M2-9QPX`.")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(WEB_CLAIM_URL, json={"code": code}, headers={"X-Sync-Secret": os.environ.get(WEB_SECRET_ENV, "")},
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    result = {}
                status = resp.status
    except Exception as e:
        print(f"⚠️ Santa: couldn't reach the website to collect a sign-up ({type(e).__name__})")
        return (False, "I couldn't reach the website just now. Please try again in a minute.")
    if status == 404:
        return (False, "I couldn't find that code. It may have expired (they last 48 hours) or been used already. Fill in the form again to get a new one.")
    if status != 200 or not isinstance(result, dict) or not result.get("success"):
        print(f"⚠️ Santa: the website refused a sign-up lookup (HTTP {status})")
        return (False, "Something went wrong collecting that sign-up. Please try again, or use the 🎁 button instead.")
    return (True, result.get("entry") or {})


def _claim_allowed(user_id):
    now = time.monotonic()
    recent = [t for t in _claim_attempts.get(user_id, []) if now - t < CLAIM_WINDOW_SECONDS]
    if len(recent) >= CLAIM_ATTEMPTS:
        _claim_attempts[user_id] = recent
        return False
    recent.append(now)
    _claim_attempts[user_id] = recent
    return True


# ============================================================
#  BACKGROUND LOOP: reminders, and keeping the website and counts fresh
# ============================================================

async def remind_givers(event, reason="reminder"):
    """DMs everyone who hasn't marked their gift sent. Returns (delivered, failed). Never names anyone's giftee."""
    noun = _kind(event)["noun"]
    delivered = failed = 0
    for g in givers_not_sent(event["id"]):
        ok = await _dm(g["giver_id"], f"🎁 A friendly reminder from the Secret Santa **{event['name']}**: your {noun} is due by "
                                      f"{_ts(event['exchange_by'], 'F')}. Press the 🎅 button on the announcement to see your giftee, "
                                      f"and the 📦 button once it's sent!")
        delivered += ok
        failed += (not ok)
    return (delivered, failed)


def _clear_dirty(event_id):
    with _db() as conn:
        conn.execute("UPDATE st_events SET web_dirty = 0 WHERE id = ?", (event_id,))
        conn.commit()


async def santa_tick(now=None):
    now = now or _now()
    with _db() as conn:
        waiting = [dict(r) for r in conn.execute("SELECT * FROM st_events WHERE status = 'scheduled' ORDER BY id").fetchall()]
    for event in waiting:
        try:
            await _open_scheduled(event, now)
        except Exception as e:
            print(f"⚠️ Santa: couldn't open scheduled exchange {event['id']} ({type(e).__name__})")
    for event in active_events():
        try:
            await _tick_one(event, now)
        except Exception as e:
            print(f"⚠️ Santa: problem with exchange {event['id']} ({type(e).__name__})")


_open_warned = set()


async def _open_scheduled(event, now):
    """On the opening day: posts the announcement and starts taking sign-ups."""
    if now < _parse(event["opens_at"]):
        return
    if now >= _parse(event["signups_close_at"]):
        cancel_event(event["id"], now)
        await _mod_log(f"⚠️ **{event['name']}** was supposed to open, but its sign-up deadline had already passed, so it was cancelled.")
        return
    with _txn() as conn:
        cur = conn.execute("UPDATE st_events SET status = 'signups', web_dirty = 1 WHERE id = ? AND status = 'scheduled'", (event["id"],))
        if cur.rowcount == 0:
            return                                          # another pass already opened it
    event = get_event(event["id"])
    channel = await _get_channel(event["announce_channel_id"])
    try:
        message = await post_announcement(event, channel)
    except Exception as e:
        print(f"⚠️ Santa: couldn't post the opening announcement ({type(e).__name__})")
        with _db() as conn:
            conn.execute("UPDATE st_events SET status = 'scheduled' WHERE id = ?", (event["id"],))
            conn.commit()
        if event["id"] not in _open_warned:
            _open_warned.add(event["id"])
            await _mod_log(f"⚠️ I couldn't post the opening announcement for **{event['name']}** (check that I can send messages and embeds "
                           f"in the channel). I'll keep trying every minute. You can also cancel it with `/santa cancel`.")
        return
    set_announcement(event["id"], channel.id, message.id)
    await push_web(event["id"])
    await _mod_log(f"🎄 **{event['name']}** is now open for sign-ups.")


async def _tick_one(event, now):
    # the moment sign-ups close: say so once, and let the mods know it's ready to draw
    if event["status"] == "signups" and not event["closed_notice"] and now >= _parse(event["signups_close_at"]):
        with _db() as conn:
            cur = conn.execute("UPDATE st_events SET closed_notice = 1, web_dirty = 1 WHERE id = ? AND closed_notice = 0", (event["id"],))
            conn.commit()
            first = cur.rowcount > 0
        if first:
            event["web_dirty"] = 1
            n = counts(event["id"])["signed_up"]
            channel = await _get_channel(event["announce_channel_id"])
            if channel is not None:
                try:
                    await channel.send(f"🔒 **Sign-ups for {event['name']} are now closed!** {n} {'person' if n == 1 else 'people'} signed up. "
                                       f"The matches will be drawn soon.")
                except Exception:
                    pass
            await _mod_log(f"🔒 Sign-ups for **{event['name']}** have closed with **{n}** signed up. "
                           + ("When you're ready, run `/santa draw` and choose it." if n >= MIN_PLAYERS
                              else f"That's fewer than the {MIN_PLAYERS} needed, so it can't be drawn yet."))
    # reminders before the deadline: one a week out and one two days out. If the bot was offline and both
    # are due at once, only a single reminder goes out.
    if event["status"] == "drawn":
        deadline = _parse(event["exchange_by"])
        due = [column for days, column in ((REMINDER_DAYS[0], "reminded_7"), (REMINDER_DAYS[1], "reminded_2"))
               if not event[column] and now >= deadline - timedelta(days=days)]
        if due:
            with _db() as conn:
                for column in due:
                    conn.execute(f"UPDATE st_events SET {column} = 1 WHERE id = ?", (event["id"],))
                conn.commit()
            if now < deadline:
                await remind_givers(event)
                waiting = len(givers_not_sent(event["id"]))
                channel = await _get_channel(event["announce_channel_id"]) if waiting else None
                if channel is not None:
                    try:
                        await channel.send(f"⏰ **{event['name']}**: {waiting} gift{'s' if waiting != 1 else ''} still to send, "
                                           f"due {_ts(event['exchange_by'], 'R')}!")
                    except Exception:
                        pass
    # keep the announcement's counts and the website current (at most every WEB_PUSH_MIN_SECONDS)
    if event["web_dirty"]:
        last = _last_web_push.get(event["id"])
        if last is None or time.monotonic() - last >= WEB_PUSH_MIN_SECONDS:
            await refresh_announcement(event["id"])
            if web_enabled() and event["public_page"]:
                if not await push_web(event["id"]):
                    _last_web_push[event["id"]] = time.monotonic()      # the website is down: wait before trying again
            else:
                _clear_dirty(event["id"])


async def santa_loop():
    await _bot.wait_until_ready()
    while not _bot.is_closed():
        try:
            await santa_tick()
        except Exception as e:
            print(f"⚠️ Santa: loop error ({type(e).__name__})")
        await asyncio.sleep(LOOP_SECONDS)


# ============================================================
#  COMMANDS
# ============================================================


# ---------------------------------------------------------------- choosing which exchange a command is about

def _kind_label(event):
    return _kind(event)["label"]


def _running_list(events):
    return "\n".join(f"• **{e['name']}** ({_kind_label(e)})" for e in events)


async def _pick_event(interaction, exchange, statuses=None, mine=False, none_text=None, include_scheduled=False):
    """Works out which exchange a command means. Returns the event, or None after telling the person what to do.
    exchange: what they chose in the pop-up list (an id) or typed (a name).
    statuses: only exchanges in these stages count.  mine: with no choice made, use the one they're in."""
    events = active_events(include_scheduled)
    if exchange:
        text = str(exchange).strip()
        match = None
        if text.isdigit():
            match = next((e for e in events if e["id"] == int(text)), None)
        if match is None:
            exact = [e for e in events if e["name"].lower() == text.lower()]
            partial = [e for e in events if text.lower() in e["name"].lower()]
            match = exact[0] if len(exact) == 1 else (partial[0] if len(partial) == 1 and not exact else None)
        if match is None:
            await interaction.response.send_message("⚠️ I couldn't tell which exchange you mean. Pick one from the list that pops up as you type.\n"
                                                    + (_running_list(events) if events else ""), ephemeral=True)
            return None
        if statuses and match["status"] not in statuses:
            await interaction.response.send_message(none_text or f"⚠️ That can't be done for **{match['name']}** right now (it's in the '{match['status']}' stage).", ephemeral=True)
            return None
        return match
    if statuses:
        events = [e for e in events if e["status"] in statuses]
    if not events:
        await interaction.response.send_message(none_text or "There's no Secret Santa running right now.", ephemeral=True)
        return None
    if len(events) == 1:
        return events[0]
    if mine:
        joined = [e for e in events if get_entry(e["id"], interaction.user.id)]
        if len(joined) == 1:
            return joined[0]
        if not joined:
            await interaction.response.send_message("You're not signed up for any of the running exchanges:\n" + _running_list(events), ephemeral=True)
            return None
        events = joined
    await interaction.response.send_message("⚠️ More than one exchange fits, so please choose one in the **exchange** box:\n" + _running_list(events), ephemeral=True)
    return None


async def _exchange_autocomplete(interaction: discord.Interaction, current: str):
    events = active_events()
    joined = {e["id"] for e in events if get_entry(e["id"], interaction.user.id)}
    events.sort(key=lambda e: (e["id"] not in joined, e["id"]))
    needle = (current or "").lower()
    out = []
    for e in events:
        label = f"{e['name']} ({_kind_label(e)})" + (" ✓" if e["id"] in joined else "")
        if needle in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=str(e["id"])))
    return out[:25]


EXCHANGE_HELP = "Which exchange (only needed when more than one is running)"


santa_group = app_commands.Group(name="santa", description="Secret Santa gift and art exchanges", guild_only=True)


class _ConfirmView(discord.ui.View):
    """A Confirm / Cancel step for actions that can't be undone."""

    def __init__(self, user_id, action):
        super().__init__(timeout=180)
        self.user_id, self.action, self.used = user_id, action, False

    @discord.ui.button(label="Yes, do it", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or not _is_mod(interaction.user):
            await interaction.response.send_message("⚠️ This belongs to the mod who opened it.", ephemeral=True)
            return
        if self.used:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Working…", view=None)
        await interaction.edit_original_response(content=await self.action())

    @discord.ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⚠️ This belongs to the mod who opened it.", ephemeral=True)
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Okay, nothing changed.", view=None)


def _kind_choices():
    return [app_commands.Choice(name=v["label"], value=k) for k, v in KINDS.items()]


@santa_group.command(name="start", description="[Mod] Start a Secret Santa exchange (you can run several at once)")
@app_commands.describe(
    name="What it's called, like 'Winter Secret Santa 2026'",
    kind="Gift, NP, NC or art exchange (changes what people are asked). You can run several at once",
    signups_until="When sign-ups close (Pacific time): 2026-12-01 closes 11:59 PM, or 2026-12-01 6:00 PM for an exact time",
    exchange_by="Day gifts or art are due, as YYYY-MM-DD (11:59 PM Pacific)",
    budget="Guidelines, like 'Up to 1 million NP in items' or 'one piece of art, SFW'",
    rules="Any extra rules",
    announce_channel="Where to post (default: this channel)",
    public_page="Also show an event page on the website? (default yes)",
    opens_on="Open sign-ups later (Pacific time): 2026-11-20 opens at 9:00 AM, or 2026-11-20 6:00 PM. Blank = open now",
    ping="A role to @mention when sign-ups open",
)
@app_commands.choices(kind=_kind_choices())
async def santa_start(interaction: discord.Interaction, name: app_commands.Range[str, 1, 80], kind: app_commands.Choice[str],
                      signups_until: str, exchange_by: str, budget: Optional[app_commands.Range[str, 0, BUDGET_MAX]] = None,
                      rules: Optional[app_commands.Range[str, 0, RULES_MAX]] = None,
                      announce_channel: Optional[discord.TextChannel] = None, public_page: bool = True,
                      opens_on: Optional[str] = None, ping: Optional[discord.Role] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    try:
        close_at, due_at = parse_pacific(signups_until), _end_of_pacific_day(exchange_by)
        open_at = parse_pacific(opens_on, (9, 0, 0)) if opens_on and opens_on.strip() else None
    except ValueError:
        await interaction.response.send_message("⚠️ Dates should look like `2026-12-01`, or with a time: `2026-12-01 6:00 PM` (Pacific time).", ephemeral=True)
        return
    now = _now()
    if open_at is not None and open_at >= close_at - timedelta(hours=1):
        await interaction.response.send_message("⚠️ Sign-ups need to open before they close (with at least an hour to spare).", ephemeral=True)
        return
    if close_at <= now + timedelta(hours=1):
        await interaction.response.send_message("⚠️ The sign-up deadline has to be in the future.", ephemeral=True)
        return
    if due_at <= close_at:
        await interaction.response.send_message("⚠️ Gifts must be due after sign-ups close.", ephemeral=True)
        return
    channel = announce_channel or interaction.channel
    ok, event_id = create_event(name.strip(), kind.value, interaction.user.id, getattr(channel, "id", None), close_at, due_at,
                                (budget or "").strip() or None, (rules or "").strip() or None, public_page, now,
                                opens_at=open_at, ping_role_id=ping.id if ping else None)
    if not ok:
        await interaction.response.send_message(f"⚠️ {event_id}", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    event = get_event(event_id)
    if event["status"] == "scheduled":
        await interaction.followup.send(
            f"🗓️ **{name.strip()}** is scheduled. I'll post the announcement in {getattr(channel, 'mention', 'that channel')} on {_ts(event['opens_at'], 'F')} "
            f"({_ts(event['opens_at'], 'R')}), and sign-ups will close {_ts(_iso(close_at), 'F')}." + (f" I'll mention {ping.mention}." if ping else "")
            + "\nChanged your mind? `/santa cancel` works until it opens.", ephemeral=True)
        return
    try:
        message = await post_announcement(event, channel)
    except Exception as e:
        print(f"⚠️ Santa: couldn't post the announcement ({type(e).__name__})")
        cancel_event(event_id, now)
        await interaction.followup.send("⚠️ I couldn't post in that channel (check that I can send messages and embeds there). Nothing was started.", ephemeral=True)
        return
    set_announcement(event_id, channel.id, message.id)
    await push_web(event_id)
    link = web_link(event)
    await interaction.followup.send(
        f"✅ **{name.strip()}** is open for sign-ups until {_ts(_iso(close_at))}." + (f"\n🌐 Event page: {link}" if link else "")
        + f"\nWhen you're ready, run `/santa draw` and choose **{name.strip()}**.", ephemeral=True)


@santa_group.command(name="join", description="Sign up (or edit your entry). Have a code from the website? Add it here")
@app_commands.describe(code="The code from the website sign-up form, like K7M2-9QPX", exchange=EXCHANGE_HELP)
async def santa_join(interaction: discord.Interaction, code: Optional[str] = None, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction):
        return
    if not code:
        event = await _pick_event(interaction, exchange)
        if event:
            await open_entry_form(interaction, event)
        return
    if getattr(interaction.user, "bot", False):
        await interaction.response.send_message("⚠️ Bots can't take part.", ephemeral=True)
        return
    if not _claim_allowed(interaction.user.id):
        await interaction.response.send_message("⚠️ That's a lot of tries. Please wait a few minutes and check the code.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, result = await claim_web_signup(code)
    if not ok:
        await interaction.followup.send(f"⚠️ {result}", ephemeral=True)
        return
    # the code itself says which exchange it's for
    event = get_event(result.get("event_id")) if isinstance(result.get("event_id"), int) else None
    if event is None or event["status"] not in ("signups", "drawn", "revealed"):
        await interaction.followup.send("⚠️ That code was for an exchange that isn't running any more.", ephemeral=True)
        return
    existing = get_entry(event["id"], interaction.user.id)
    if not existing and (event["status"] != "signups" or _now() >= _parse(event["signups_close_at"])):
        await interaction.followup.send(f"⚠️ Sign-ups for **{event['name']}** have closed.", ephemeral=True)
        return
    valid, cleaned = validate_entry(result.get("neopets_name"), result.get("wants"), result.get("links"), result.get("avoid"))
    if not valid:
        await interaction.followup.send(f"⚠️ {cleaned} Please press the 🎁 button on the **{event['name']}** announcement instead.", ephemeral=True)
        return
    saved, outcome = upsert_entry(event["id"], interaction.user.id, interaction.user.display_name, cleaned, "web", _now())
    await interaction.followup.send((f"**{event['name']}**\n" + _entry_saved_text(outcome)) if saved else f"⚠️ {outcome}", ephemeral=True)


@santa_group.command(name="list", description="See the exchanges that are running and which ones you're in")
async def santa_list(interaction: discord.Interaction):
    if await _deny_wrong_guild(interaction):
        return
    events = active_events(True)
    if not events:
        await interaction.response.send_message("There's no Secret Santa running right now.", ephemeral=True)
        return
    lines = []
    for e in events:
        mine = get_entry(e["id"], interaction.user.id)
        stage = {"signups": "sign-ups open", "drawn": "matches drawn", "revealed": "revealed", "scheduled": "coming soon"}.get(e["status"], e["status"])
        line = f"{'✅' if mine else '▫️'} **{e['name']}** ({_kind_label(e)}): {stage}"
        if e["status"] == "scheduled":
            line += f", sign-ups open {_ts(e['opens_at'], 'R')}"
        elif e["status"] == "signups" and _now() >= _parse(e["signups_close_at"]):
            line += ", sign-ups closed"
        elif e["status"] == "signups":
            line += f", sign-ups close {_ts(e['signups_close_at'], 'R')}"
        elif e["status"] == "drawn":
            line += f", due {_ts(e['exchange_by'], 'R')}"
        lines.append(line)
    lines.append("\n✅ = you're in. You can join as many as you like!")
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@santa_group.command(name="leave", description="Withdraw from the exchange (before the matches are drawn)")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_leave(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=True)
    if not event:
        return
    ok, why = leave_event(event["id"], interaction.user.id)
    await interaction.response.send_message("👋 You've been removed. You can sign up again until sign-ups close." if ok else f"⚠️ {why}", ephemeral=True)


@santa_group.command(name="me", description="See your entry and where things stand")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_me(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=True)
    if not event:
        return
    mine = get_entry(event["id"], interaction.user.id)
    if not mine:
        await interaction.response.send_message("You're not signed up. Press the 🎁 button on the announcement or use `/santa join`.", ephemeral=True)
        return
    lines = [f"🎅 **{event['name']}**", f"Your Neopets username: **{mine['neopets_name']}**", f"**You asked for:** {mine['wants'] or '—'}"]
    if mine["links"]:
        lines.append(f"**Links:** {mine['links']}")
    if mine["avoid"]:
        lines.append(f"**Please avoid:** {mine['avoid']}")
    giver_row = match_row_as_giver(event["id"], interaction.user.id)
    if event["status"] == "signups":
        lines.append("The matches haven't been drawn yet.")
    elif giver_row:
        lines.append("📦 You've sent your gift." if giver_row["sent_at"] else "You haven't marked your gift as sent yet.")
        received = match_row_as_giftee(event["id"], interaction.user.id)
        lines.append("💌 You've marked your gift as received." if received and received["received_at"] else "Your own gift hasn't been marked as received yet.")
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@santa_group.command(name="match", description="See who you're the Secret Santa for")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_match(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=True)
    if not event:
        return
    await show_match(interaction, event)


@santa_group.command(name="sent", description="Mark your gift as sent")
@app_commands.describe(exchange=EXCHANGE_HELP)
@app_commands.describe(undo="Set this to un-mark it")
async def santa_sent(interaction: discord.Interaction, undo: bool = False, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=True)
    if not event:
        return
    await do_mark_sent(interaction, event, undo)


@santa_group.command(name="received", description="Say your gift arrived, with an optional thank-you")
@app_commands.describe(exchange=EXCHANGE_HELP)
@app_commands.describe(thanks="A thank-you note for your Santa (shown at the reveal)")
async def santa_received(interaction: discord.Interaction, thanks: Optional[app_commands.Range[str, 0, THANKS_MAX]] = None, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=True)
    if not event:
        return
    ok, result = mark_received(event["id"], interaction.user.id, _now(), thanks)
    await interaction.response.send_message(("💌 Marked as received. Thank you!" if result == "marked" else "✅ Already marked. Your note was updated.")
                                            if ok else f"⚠️ {result}", ephemeral=True)


@santa_group.command(name="block", description="[Mod] Make sure two members are never matched with each other")
@app_commands.describe(exchange=EXCHANGE_HELP)
@app_commands.describe(member_a="First member", member_b="Second member")
async def santa_block(interaction: discord.Interaction, member_a: discord.Member, member_b: discord.Member, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=False)
    if not event:
        return
    ok, why = add_block(event["id"], member_a.id, member_b.id)
    await interaction.response.send_message(
        f"✅ {member_a.display_name} and {member_b.display_name} won't be matched with each other (in either direction)." if ok else f"⚠️ {why}",
        ephemeral=True)


@santa_group.command(name="unblock", description="[Mod] Remove a blocked pair")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_unblock(interaction: discord.Interaction, member_a: discord.Member, member_b: discord.Member, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=False)
    if not event:
        return
    ok, why = remove_block(event["id"], member_a.id, member_b.id)
    await interaction.response.send_message("✅ Unblocked." if ok else f"⚠️ {why}", ephemeral=True)


@santa_group.command(name="draw", description="[Mod] Draw the matches (everyone gets someone; nobody gets themselves)")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_draw(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, statuses=("signups",), none_text="⚠️ There's no exchange waiting to be drawn.")
    if not event:
        return
    n = len(entries_for(event["id"]))
    blocks = len(blocked_pairs(event["id"]))
    if n < MIN_PLAYERS:
        await interaction.response.send_message(f"⚠️ Only {n} signed up. You need at least {MIN_PLAYERS}.", ephemeral=True)
        return
    early = _now() < _parse(event["signups_close_at"])

    async def do_draw():
        ok, result = draw_event(event["id"], _now())
        if not ok:
            return f"⚠️ {result}"
        await refresh_announcement(event["id"])
        channel = await _get_channel(event["announce_channel_id"])
        if channel is not None:
            try:
                await channel.send(f"🎅 **The matches for {event['name']} are out!** Press **🎅 My giftee** on the announcement to see who you're giving to. "
                                   f"Remember: it's a secret! 🤫")
            except Exception:
                pass
        await push_web(event["id"])
        return f"🎅 Done! {result['players']} people matched. Everyone can now see their giftee with the 🎅 button or `/santa match`."

    await interaction.response.send_message(
        f"Draw the matches for **{event['name']}** now? **{n}** people are signed up" + (f" and **{blocks}** pair{'s are' if blocks != 1 else ' is'} blocked" if blocks else "")
        + "." + (f"\n⚠️ Sign-ups are still open until {_ts(event['signups_close_at'])}. Drawing closes them early." if early else "")
        + "\nThis can't be redone.", view=_ConfirmView(interaction.user.id, do_draw), ephemeral=True)


def _status_text(event):
    c = counts(event["id"])
    text = (f"🎅 **{event['name']}** ({_kind_label(event)}): {event['status']}"
            + (f", opens {_ts(event['opens_at'], 'F')}" if event["status"] == "scheduled" else "") + f"\nSigned up: {c['signed_up']} · Blocked pairs: {len(blocked_pairs(event['id']))}\n"
            f"Sign-ups close {_ts(event['signups_close_at'])} · Due {_ts(event['exchange_by'])}")
    if event["status"] != "signups":
        text += f"\nMatched: {c['matched']} · Sent: {c['sent']} · Received: {c['received']}"
    return text


@santa_group.command(name="status", description="[Mod] How the exchange(s) are going (counts only, no names)")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_status(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    if not exchange and len(active_events(True)) > 1:
        text = "\n\n".join(_status_text(e) for e in active_events(True))
        await interaction.response.send_message(text[:1990], ephemeral=True)
        return
    event = await _pick_event(interaction, exchange, include_scheduled=True)
    if not event:
        return
    await interaction.response.send_message(_status_text(event), ephemeral=True)


@santa_group.command(name="remind", description="[Mod] Send a reminder to everyone who hasn't marked their gift sent")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_remind(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, statuses=("drawn",),
                              none_text="⚠️ Reminders only make sense after the draw and before the reveal.")
    if not event:
        return
    await interaction.response.defer(ephemeral=True)
    delivered, failed = await remind_givers(event)
    await interaction.followup.send(f"📨 Reminded {delivered} people." + (f" {failed} have private messages turned off, so I couldn't reach them." if failed else ""), ephemeral=True)


@santa_group.command(name="lookup", description="[Mod] Emergency only: see one member's match. Always logged to the mod channel")
@app_commands.describe(exchange=EXCHANGE_HELP)
@app_commands.describe(member="The member to look up")
async def santa_lookup(interaction: discord.Interaction, member: discord.Member, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=False)
    if not event:
        return
    ok, result = lookup_match(event["id"], interaction.user.id, member.id, _now())
    if not ok:
        await interaction.response.send_message(f"⚠️ {result}", ephemeral=True)
        return
    logged = await _mod_log(f"🔍 **Secret Santa lookup #{result['lookup_number']}:** {interaction.user.display_name} looked up "
                            f"{member.display_name}'s match in **{event['name']}**.")
    await interaction.response.send_message(
        f"🔍 **{member.display_name}**\nTheir Santa: **{result['santa'] or 'nobody'}**\nThey're giving to: **{result['giftee'] or 'nobody'}**\n"
        + ("*This lookup was logged in the mod channel.*" if logged else "*I couldn't post this lookup to the mod channel, but it was recorded.*"),
        ephemeral=True)


@santa_group.command(name="dropout", description="[Mod] Remove someone, and repair the matches if they've been drawn")
@app_commands.describe(exchange=EXCHANGE_HELP)
@app_commands.describe(member="The member leaving the exchange")
async def santa_dropout(interaction: discord.Interaction, member: discord.Member, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=False)
    if not event:
        return
    if not get_entry(event["id"], member.id):
        await interaction.response.send_message("⚠️ That member isn't in this exchange.", ephemeral=True)
        return

    async def do_drop():
        result = drop_participant(event["id"], member.id)
        if isinstance(result, tuple):
            return f"⚠️ {result[1]}"
        if result["phase"] == "signups":
            return f"👋 Removed {member.display_name}."
        parts = [f"👋 Removed {member.display_name}."]
        if result["repaired"]:
            parts.append("✅ The matches were repaired" + (" (one other pair was swapped to make it work)" if result.get("swapped") else "") + ".")
            for uid in result["notify"]:
                await _dm(uid, f"🎅 A quick update from **{event['name']}**: your giftee has changed because someone had to leave. "
                               f"Press the 🎅 button on the announcement to see who you're giving to now.")
            if result["notify"]:
                parts.append(f"{len(result['notify'])} {'person was' if len(result['notify']) == 1 else 'people were'} told privately.")
            if result.get("had_sent"):
                parts.append("⚠️ Their Santa had already marked a gift as sent to the person who left, so you may want to check with them.")
        else:
            parts.append(f"⚠️ I couldn't repair the matches automatically ({result['reason']}). Someone is now without a match, so you may want to handle it by hand.")
        await _mod_log(f"👋 {interaction.user.display_name} removed {member.display_name} from **{event['name']}**."
                       + (" Matches were repaired." if result["repaired"] else " Matches could NOT be repaired automatically."))
        await refresh_announcement(event["id"])
        await push_web(event["id"])
        return " ".join(parts)

    await interaction.response.send_message(
        f"Remove **{member.display_name}** from **{event['name']}**?" + (" Their Santa and their giftee will be re-matched." if event["status"] == "drawn" else ""),
        view=_ConfirmView(interaction.user.id, do_drop), ephemeral=True)


def _batch_embeds(embeds, max_per_message=10, max_chars=5500):
    """Groups embeds into messages. Discord allows at most 10 embeds and about 6000 characters of embed text
    in one message, so a long reveal is split across several."""
    batches, current, size = [], [], 0
    for e in embeds:
        n = len(e)
        if current and (len(current) >= max_per_message or size + n > max_chars):
            batches.append(current)
            current, size = [], 0
        current.append(e)
        size += n
    if current:
        batches.append(current)
    return batches


def _reveal_embeds(event, pairs):
    embeds, lines = [], []
    for p in pairs:
        line = f"🎅 **{p['giver_name']}** ({p['giver_np']}) → 🎁 **{p['giftee_name']}** ({p['giftee_np']})"
        if p.get("note"):
            line += f"\n> 💌 “{p['note']}”"
        lines.append(line)

    def flush(chunk):
        embeds.append(discord.Embed(title=f"🎊 Reveal: {event['name']}" if not embeds else None, description="\n".join(chunk), color=EMBED_COLOR))
    chunk, size = [], 0
    for line in lines:
        if chunk and size + len(line) > 3500:
            flush(chunk)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        flush(chunk)
    return embeds


@santa_group.command(name="reveal", description="[Mod] Reveal who had whom, with any thank-you notes")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_reveal(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, statuses=("drawn",), none_text="⚠️ There's no drawn exchange to reveal.")
    if not event:
        return
    c = counts(event["id"])

    async def do_reveal():
        ok, why = reveal_event(event["id"], _now())
        if not ok:
            return f"⚠️ {why}"
        channel = await _get_channel(event["announce_channel_id"])
        pairs = reveal_pairs(event["id"])
        if channel is not None:
            try:
                for batch in _batch_embeds(_reveal_embeds(event, pairs)):
                    await channel.send(embeds=batch, allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:
                print(f"⚠️ Santa: couldn't post the reveal ({type(e).__name__})")
        await refresh_announcement(event["id"])
        await push_web(event["id"])
        return "🎊 The reveal is posted! When you're done, run `/santa close` to end the exchange and erase everyone's wishlists."

    await interaction.response.send_message(
        f"Reveal **everyone's** matches for **{event['name']}** publicly? {c['sent']} of {c['matched']} gifts are marked sent. This can't be undone.",
        view=_ConfirmView(interaction.user.id, do_reveal), ephemeral=True)


@santa_group.command(name="close", description="[Mod] End the exchange and erase everyone's wishlists and notes")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_close(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, mine=False)
    if not event:
        return

    async def do_close():
        ok, why = close_event(event["id"], _now())
        if not ok:
            return f"⚠️ {why}"
        await refresh_announcement(event["id"])
        await push_web(event["id"])
        return "🔒 The exchange is closed and everyone's wishlists, links and notes have been erased."

    await interaction.response.send_message(
        f"Close **{event['name']}**? This erases everyone's wishlists, links and notes. The pairs from the reveal stay on record.",
        view=_ConfirmView(interaction.user.id, do_close), ephemeral=True)


@santa_group.command(name="cancel", description="[Mod] Cancel an exchange that hasn't been drawn yet")
@app_commands.describe(exchange=EXCHANGE_HELP)
async def santa_cancel(interaction: discord.Interaction, exchange: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    event = await _pick_event(interaction, exchange, statuses=("signups", "scheduled"), include_scheduled=True,
                              none_text="⚠️ Only an exchange that hasn't been drawn can be cancelled.")
    if not event:
        return

    async def do_cancel():
        ok, why = cancel_event(event["id"], _now())
        if not ok:
            return f"⚠️ {why}"
        if event["status"] == "scheduled":
            return "🚫 Cancelled. It hadn't opened yet, so nothing was posted."
        await refresh_announcement(event["id"])
        await push_web(event["id"])
        return "🚫 Cancelled. Everyone's entries were erased."

    await interaction.response.send_message(f"Cancel **{event['name']}**? Everyone's entries will be erased.",
                                            view=_ConfirmView(interaction.user.id, do_cancel), ephemeral=True)


for _cmd in (santa_join, santa_leave, santa_me, santa_match, santa_sent, santa_received, santa_block, santa_unblock,
             santa_draw, santa_status, santa_remind, santa_lookup, santa_dropout, santa_reveal, santa_close, santa_cancel):
    _cmd.autocomplete("exchange")(_exchange_autocomplete)


# ============================================================
#  SETUP - called from main.py
# ============================================================

def setup(bot, get_db, mod_roles, main_guild_name, mod_channel_name=None):
    global _bot, _get_db_fn, _mod_roles, _guild_names, _mod_channel_name
    _bot = bot
    _get_db_fn = get_db
    _mod_roles = list(mod_roles)
    _guild_names = [main_guild_name] + list(SANTA_GUILD_NAMES_EXTRA)
    _mod_channel_name = mod_channel_name
    init_tables()
    bot.tree.add_command(santa_group)
    print("🎅 Secret Santa ready")


def start_background(bot):
    """Call from on_ready: re-attaches the buttons on announcements made before a restart, and starts the loop
    that sends reminders and keeps the website current."""
    bot.add_view(SantaView())
    bot.loop.create_task(santa_loop())
