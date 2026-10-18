"""
contest.py - Secret-ballot voting for any kind of guild contest (costumes, art, crafts, poetry, ...)
===================================================================================

HOW A CONTEST RUNS
1. A mod creates a contest:        /contest create    (choose the kind: costume, art, craft, poetry, other)
2. A mod adds each entry:          /contest addentry   (its title + the member who entered it;
                                                   poems open a pop-up to paste the text into)
3. A mod opens voting:             /contest open       (posts the entries and a Vote button)
4. Members vote with the button (or /vote). Each member awards:
       Gold   = 1st choice = 10 points
       Silver = 2nd choice =  5 points
       Copper = 3rd choice =  2 points
   Members can never award a medal to an entry they entered themselves.
5. When the voting period ends (or a mod closes it early) the bot adds up the
   points and posts the top three. /contest standings shows mods the full tally.

PRIVACY: WHO VOTED FOR WHOM IS NOT RECORDED ANYWHERE
The database keeps two things that are deliberately NOT connected:
  - a list of WHO HAS VOTED (so nobody votes twice). It has no choices in it, and
    it is stored sorted by member, not in the order people voted.
  - a pile of BALLOTS (gold / silver / copper entry). They have no name and no
    timestamp on them.
Looking at the database, nobody (including whoever runs the bot) can tell which
ballot belongs to which member. The bot also never prints or logs votes.
Because a ballot can't be traced back to its voter, votes are final: a member
reviews their ballot, then casts it once.

BIGGER CONTESTS AND SHARING
- Up to 25 entries: the ballot is three dropdowns. More than that: a paged ballot (medal tabs
  plus arrows), because Discord dropdowns hold at most 25 choices.
- The bot can publish a public web gallery of the entries (a page on your website with a
  link you can share anywhere). Only the entries and, once the contest is over, the final top
  three are sent there. It never receives who voted or how anyone voted.

HOOKS NEEDED IN main.py (3 small edits):
    import contest                                                       (near the top)
    contest.setup(bot, get_db, BOT_MOD_ROLES, MAIN_GUILD_NAME)           (just before bot.run)
    contest.start_background(bot)                                        (in on_ready, with the other tasks)
"""

import asyncio
import math
import os
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

# The main server's name is added automatically. Add a test server's exact name
# here if you want to try contests there too.
CONTEST_GUILD_NAMES_EXTRA = []

# Role names allowed to vote. Leave empty so every member can vote.
CONTEST_VOTER_ROLES = []

GOLD_POINTS = 10
SILVER_POINTS = 5
COPPER_POINTS = 2

MAX_ENTRIES = 100              # most entries one contest can have
OPTIONS_PER_PAGE = 25          # Discord's dropdowns hold at most 25 choices, so bigger contests are paged
GALLERY_EMBED_LIMIT = 25       # up to this many entries are posted as picture cards in the channel;
                               # bigger contests post a compact list and point to the web gallery instead
DEFAULT_VOTING_HOURS = 72
CONTEST_LOOP_SECONDS = 60      # how often the bot checks whether voting has ended
MAX_RESULT_ATTEMPTS = 5        # tries to post results before giving up

# The public web gallery (a page on your website that shows every entry, easy to share).
# It reuses ACTIVITY_SYNC_SECRET, the shared secret your other website syncs use.
# Set CONTEST_WEB_SYNC_URL to an empty string to switch the web gallery off entirely.
WEB_SYNC_URL = os.environ.get("CONTEST_WEB_SYNC_URL", "https://athenaeumarchive.com/contests/contest_sync.php")
WEB_PUBLIC_URL = os.environ.get("CONTEST_PUBLIC_URL", "https://athenaeumarchive.com/contests/")
WEB_SECRET_ENV = "ACTIVITY_SYNC_SECRET"
WEB_RETRY_MINUTES = 5          # if the website doesn't answer, try again this often...
WEB_MAX_ATTEMPTS = 12          # ...up to this many times (about an hour), then wait for the next change

EMBED_COLOR = 0xD68A4E
PACIFIC = ZoneInfo("America/Los_Angeles")

# (key, medal, name) in order of value
TIERS = [("gold", "🥇", "Gold"), ("silver", "🥈", "Silver"), ("copper", "🥉", "Copper")]

# CONTEST TYPES. The type sets the words used in messages ("Pick a different poem...") and whether
# entries are made of text (poems). Everything else (the medals, secret ballots, rules) is the same.
# To add another type, add a line here and a matching choice on /contest create.
KINDS = {
    "costume": {"label": "Costume contest (pets)", "singular": "pet",   "plural": "pets",    "text": False},
    "art":     {"label": "Art contest",            "singular": "piece", "plural": "pieces",  "text": False},
    "craft":   {"label": "Craft contest",          "singular": "craft", "plural": "crafts",  "text": False},
    "poetry":  {"label": "Poetry contest",         "singular": "poem",  "plural": "poems",   "text": True},
    "other":   {"label": "Other contest",          "singular": "entry", "plural": "entries", "text": False},
}
MAX_EXTEND_DAYS = 90          # a deadline can't be pushed further than this from now
MAX_BODY_CHARS = 4000          # longest text an entry can have (a poem)
EMBED_TEXT_CHARS = 1500        # how much of a poem is shown in each channel post (the web page shows it all)


def plural_of(word):
    """A simple plural for a custom word ("recipe" -> "recipes", "story" -> "stories")."""
    w = word.strip()
    if len(w) > 1 and w.endswith("y") and w[-2].lower() not in "aeiou":
        return w[:-1] + "ies"
    if w.lower().endswith(("s", "x", "z", "ch", "sh")):
        return w + "es"
    return w + "s"


def words(contest):
    """(singular, plural) for a contest. Contests made before types existed read as pet costume contests."""
    return (contest.get("entry_singular") or "pet", contest.get("entry_plural") or "pets")

# ============================================================
#  STATE (filled in by setup())
# ============================================================

_bot = None
_get_db_fn = None
_mod_roles = []
_guild_names = []
_web_next_try = {}      # contest id -> when to try the website again
_web_failures = {}      # contest id -> how many tries have failed in a row


def _now():
    return datetime.now(timezone.utc)


# ============================================================
#  TIME HELPERS (fixed-format UTC text, so text order == time order)
# ============================================================

_FMT = "%Y-%m-%d %H:%M:%S"


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime(_FMT)


def _parse(s):
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


def _ts(s, style="f"):
    return f"<t:{int(_parse(s).timestamp())}:{style}>"


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
    """A write transaction that locks the database up front, so two people voting at the
    same instant can't slip past each other's checks."""
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
            CREATE TABLE IF NOT EXISTS ct_contests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'setup',
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                announce_channel_id INTEGER,
                show_owners INTEGER NOT NULL DEFAULT 1,
                gold_pts INTEGER NOT NULL,
                silver_pts INTEGER NOT NULL,
                copper_pts INTEGER NOT NULL,
                voting_ends_at TEXT,
                vote_channel_id INTEGER,
                vote_message_id INTEGER,
                closed_at TEXT,
                results_posted INTEGER NOT NULL DEFAULT 0,
                results_attempts INTEGER NOT NULL DEFAULT 0,
                public_page INTEGER NOT NULL DEFAULT 1,
                web_synced_status TEXT,
                kind TEXT NOT NULL DEFAULT 'costume',
                entry_singular TEXT NOT NULL DEFAULT 'pet',
                entry_plural TEXT NOT NULL DEFAULT 'pets',
                text_entries INTEGER NOT NULL DEFAULT 0,
                results_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS ct_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contest_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                pet_name TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                image_url TEXT,
                description TEXT,
                body TEXT,
                UNIQUE (contest_id, number)
            );
            -- WHO HAS VOTED. No choices here. WITHOUT ROWID stores rows sorted by
            -- member id, so the table does not reveal the order people voted in.
            CREATE TABLE IF NOT EXISTS ct_voters (
                contest_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (contest_id, user_id)
            ) WITHOUT ROWID;
            -- WHAT WAS CHOSEN. No member id and no timestamp on any ballot.
            CREATE TABLE IF NOT EXISTS ct_ballots (
                contest_id INTEGER NOT NULL,
                gold_entry_id INTEGER,
                silver_entry_id INTEGER,
                copper_entry_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_ct_entries_contest ON ct_entries(contest_id);
            CREATE INDEX IF NOT EXISTS idx_ct_ballots_contest ON ct_ballots(contest_id);
        """)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ct_contests)").fetchall()}
        if "public_page" not in cols:
            conn.execute("ALTER TABLE ct_contests ADD COLUMN public_page INTEGER NOT NULL DEFAULT 1")
        if "web_synced_status" not in cols:
            conn.execute("ALTER TABLE ct_contests ADD COLUMN web_synced_status TEXT")
        for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'costume'"), ("entry_singular", "TEXT NOT NULL DEFAULT 'pet'"),
                          ("entry_plural", "TEXT NOT NULL DEFAULT 'pets'"), ("text_entries", "INTEGER NOT NULL DEFAULT 0")):
            if name not in cols:
                conn.execute(f"ALTER TABLE ct_contests ADD COLUMN {name} {ddl}")
        if "results_message_id" not in cols:
            conn.execute("ALTER TABLE ct_contests ADD COLUMN results_message_id INTEGER")
        entry_cols = {r["name"] for r in conn.execute("PRAGMA table_info(ct_entries)").fetchall()}
        if "body" not in entry_cols:
            conn.execute("ALTER TABLE ct_entries ADD COLUMN body TEXT")
        conn.commit()


def get_contest(contest_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
    return dict(row) if row else None


def list_contests(statuses=None):
    with _db() as conn:
        rows = conn.execute("SELECT * FROM ct_contests ORDER BY id DESC").fetchall()
    out = [dict(r) for r in rows]
    if statuses:
        out = [c for c in out if c["status"] in statuses]
    return out


def contest_by_vote_message(message_id):
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM ct_contests WHERE vote_message_id = ? AND status = 'voting'", (message_id,)
        ).fetchone()
    return dict(row) if row else None


def entries_for(contest_id):
    with _db() as conn:
        rows = conn.execute("SELECT * FROM ct_entries WHERE contest_id = ? ORDER BY number", (contest_id,)).fetchall()
    return [dict(r) for r in rows]


def create_contest(name, created_by, announce_channel_id, show_owners, now, public_page=True,
                   kind="costume", singular=None, plural=None, text_entries=None):
    """Makes a contest in setup. The type (kind) fills in the words and whether entries are text;
    singular / plural / text_entries can override what the type says."""
    preset = KINDS.get(kind, KINDS["other"])
    singular = (singular or "").strip() or preset["singular"]
    plural = (plural or "").strip() or (preset["plural"] if singular == preset["singular"] else plural_of(singular))
    text_flag = preset["text"] if text_entries is None else bool(text_entries)
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO ct_contests (name, status, created_by, created_at, announce_channel_id, show_owners, "
            "gold_pts, silver_pts, copper_pts, public_page, kind, entry_singular, entry_plural, text_entries) "
            "VALUES (?, 'setup', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, created_by, _iso(now), announce_channel_id, 1 if show_owners else 0,
             GOLD_POINTS, SILVER_POINTS, COPPER_POINTS, 1 if public_page else 0,
             kind if kind in KINDS else "other", singular[:30], plural[:30], 1 if text_flag else 0)
        )
        conn.commit()
        return cur.lastrowid


def add_entry(contest_id, title, owner_id, image_url, description, body=None):
    """Adds an entry. 'title' is the pet's name, the artwork's title, the poem's title, and so on.
    Contests whose entries are text (poetry) require the text in 'body'; other contests ignore it.
    Returns (True, entry_number) or (False, reason)."""
    with _txn() as conn:
        c = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
        if not c:
            return (False, "That contest doesn't exist.")
        if c["status"] != "setup":
            return (False, "Entries can only be added before voting opens.")
        one, _many = words(dict(c))
        body = (body or "").replace("\r\n", "\n").strip()
        if c["text_entries"]:
            if not body:
                return (False, f"This contest's entries are text, so the {one}'s text is needed.")
            if len(body) > MAX_BODY_CHARS:
                return (False, f"The text can be at most {MAX_BODY_CHARS:,} characters (yours is {len(body):,}).")
        else:
            body = ""
        existing = conn.execute("SELECT number, pet_name, owner_id FROM ct_entries WHERE contest_id = ?", (contest_id,)).fetchall()
        if len(existing) >= MAX_ENTRIES:
            return (False, f"A contest can have at most {MAX_ENTRIES} entries.")
        # the same title from the same person is almost certainly a double entry; two different people can share a title
        if any(r["pet_name"].strip().lower() == title.strip().lower() and r["owner_id"] == owner_id for r in existing):
            return (False, f"There's already an entry called \"{title}\" from that member in this contest.")
        number = max([r["number"] for r in existing], default=0) + 1
        conn.execute(
            "INSERT INTO ct_entries (contest_id, number, pet_name, owner_id, image_url, description, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contest_id, number, title.strip(), owner_id, image_url or None, description or None, body or None)
        )
        return (True, number)


def set_entry_body(contest_id, number, body):
    """Replaces an entry's text (before voting opens)."""
    with _txn() as conn:
        c = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
        if not c:
            return (False, "That contest doesn't exist.")
        if c["status"] != "setup":
            return (False, "Entries can only be changed before voting opens.")
        if not c["text_entries"]:
            return (False, "This contest's entries don't have text.")
        body = (body or "").replace("\r\n", "\n").strip()
        if not body:
            return (False, "The text can't be empty.")
        if len(body) > MAX_BODY_CHARS:
            return (False, f"The text can be at most {MAX_BODY_CHARS:,} characters (yours is {len(body):,}).")
        cur = conn.execute("UPDATE ct_entries SET body = ? WHERE contest_id = ? AND number = ?", (body, contest_id, number))
        if cur.rowcount == 0:
            return (False, f"There's no entry #{number} in that contest.")
        return (True, "ok")


def remove_entry(contest_id, number):
    with _txn() as conn:
        c = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
        if not c:
            return (False, "That contest doesn't exist.")
        if c["status"] != "setup":
            return (False, "Entries can only be removed before voting opens.")
        cur = conn.execute("DELETE FROM ct_entries WHERE contest_id = ? AND number = ?", (contest_id, number))
        if cur.rowcount == 0:
            return (False, f"There's no entry #{number} in that contest.")
        return (True, "ok")


def open_contest(contest_id, ends_at, channel_id, message_id, now):
    with _txn() as conn:
        c = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
        if not c or c["status"] != "setup":
            return (False, "That contest isn't in setup, so it can't be opened.")
        n = conn.execute("SELECT COUNT(*) AS n FROM ct_entries WHERE contest_id = ?", (contest_id,)).fetchone()["n"]
        if n < 2:
            return (False, "Add at least 2 entries before opening voting.")
        conn.execute(
            "UPDATE ct_contests SET status = 'voting', voting_ends_at = ?, vote_channel_id = ?, vote_message_id = ? WHERE id = ?",
            (_iso(ends_at), channel_id, message_id, contest_id)
        )
        return (True, "ok")


def close_contest(contest_id, now):
    with _txn() as conn:
        c = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
        if not c or c["status"] != "voting":
            return (False, "That contest isn't taking votes.")
        conn.execute("UPDATE ct_contests SET status = 'closed', closed_at = ? WHERE id = ?", (_iso(now), contest_id))
        return (True, "ok")


def extend_contest(contest_id, new_end, now):
    """Gives a contest more voting time. A contest that's still taking votes just gets a later deadline;
    one that already closed is REOPENED (its results are cleared, the ballots already cast are kept).
    Returns (True, info) or (False, reason)."""
    with _txn() as conn:
        c = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
        if not c:
            return (False, "That contest doesn't exist.")
        if c["status"] not in ("voting", "closed"):
            return (False, "Only a contest that's taking votes, or one that has closed, can be extended.")
        if new_end > now + timedelta(days=MAX_EXTEND_DAYS):
            return (False, f"A deadline can't be more than {MAX_EXTEND_DAYS} days away.")
        info = {"was": c["status"], "old_end": c["voting_ends_at"], "results_message_id": c["results_message_id"]}
        if c["status"] == "voting":
            if new_end <= _parse(c["voting_ends_at"]):
                return (False, f"The new deadline has to be later than the current one ({_ts(c['voting_ends_at'])}).")
            conn.execute("UPDATE ct_contests SET voting_ends_at = ?, web_synced_status = NULL WHERE id = ?",
                         (_iso(new_end), contest_id))
        else:
            if new_end <= now + timedelta(minutes=5):
                return (False, "Reopened voting needs to run for at least a few more minutes.")
            conn.execute(
                "UPDATE ct_contests SET status = 'voting', voting_ends_at = ?, closed_at = NULL, results_posted = 0, "
                "results_attempts = 0, results_message_id = NULL, web_synced_status = NULL WHERE id = ?",
                (_iso(new_end), contest_id))
        return (True, info)


def cancel_contest(contest_id, now):
    with _txn() as conn:
        c = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
        if not c or c["status"] not in ("setup", "voting"):
            return (False, "Only contests in setup or voting can be cancelled.")
        conn.execute("UPDATE ct_contests SET status = 'cancelled', closed_at = ?, results_posted = 1 WHERE id = ?",
                     (_iso(now), contest_id))
        return (True, "ok")


def ballots_cast(contest_id):
    with _db() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM ct_ballots WHERE contest_id = ?", (contest_id,)).fetchone()["n"]


def has_voted(contest_id, user_id):
    with _db() as conn:
        return conn.execute("SELECT 1 FROM ct_voters WHERE contest_id = ? AND user_id = ?", (contest_id, user_id)).fetchone() is not None


def eligible_entries(contest_id, voter_id):
    """Entries this member is allowed to give a medal to: everything except their own."""
    return [e for e in entries_for(contest_id) if e["owner_id"] != voter_id]


def required_picks(n_eligible):
    """Everyone fills all three medals if there are at least three entries they can vote for;
    otherwise they use as many medals as there are entries."""
    return min(3, n_eligible)


def cast_ballot(contest_id, voter_id, picks, now):
    """Records one member's ballot. picks = {'gold': entry_id, 'silver': entry_id, 'copper': entry_id}.
    All checks happen BEFORE anything is written, then the 'has voted' mark and the anonymous
    ballot are written together or not at all. Returns (True, 'ok') or (False, reason)."""
    with _txn() as conn:
        c = conn.execute("SELECT * FROM ct_contests WHERE id = ?", (contest_id,)).fetchone()
        if not c or c["status"] != "voting" or not c["voting_ends_at"] or now >= _parse(c["voting_ends_at"]):
            return (False, "Voting for this contest has closed.")
        if conn.execute("SELECT 1 FROM ct_voters WHERE contest_id = ? AND user_id = ?", (contest_id, voter_id)).fetchone():
            return (False, "You've already voted in this contest. Votes are final. Thank you!")

        entries = {r["id"]: r for r in conn.execute("SELECT * FROM ct_entries WHERE contest_id = ?", (contest_id,)).fetchall()}
        eligible = [e for e in entries.values() if e["owner_id"] != voter_id]
        need = required_picks(len(eligible))
        one, _many = words(dict(c))
        if need == 0:
            return (False, "There's nothing for you to vote on in this contest.")
        tiers = [t[0] for t in TIERS][:need]
        if set(picks.keys()) != set(tiers):
            return (False, f"Please choose a {one} for each of the {need} medal{'s' if need != 1 else ''}.")
        ids = [picks[t] for t in tiers]
        if len(set(ids)) != len(ids):
            return (False, f"Each medal has to go to a different {one}.")
        for eid in ids:
            entry = entries.get(eid)
            if entry is None:
                return (False, "One of those entries doesn't exist any more.")
            if entry["owner_id"] == voter_id:
                return (False, "You can't give a medal to your own entry.")

        conn.execute("INSERT INTO ct_voters (contest_id, user_id) VALUES (?, ?)", (contest_id, voter_id))
        conn.execute(
            "INSERT INTO ct_ballots (contest_id, gold_entry_id, silver_entry_id, copper_entry_id) VALUES (?, ?, ?, ?)",
            (contest_id, picks.get("gold"), picks.get("silver"), picks.get("copper"))
        )
        return (True, "ok")


# ============================================================
#  COUNTING
# ============================================================

def rank_entries(entries, ballots, gold_pts=GOLD_POINTS, silver_pts=SILVER_POINTS, copper_pts=COPPER_POINTS):
    """Totals the points and ranks the entries.
    Order: most points; ties broken by most gold medals, then silver, then copper.
    Entries still tied after that share the same place (1, 2, 2, 4, ...)."""
    tally = {e["id"]: {"entry": e, "gold": 0, "silver": 0, "copper": 0} for e in entries}
    for b in ballots:
        for key, column in (("gold", "gold_entry_id"), ("silver", "silver_entry_id"), ("copper", "copper_entry_id")):
            eid = b[column]
            if eid in tally:
                tally[eid][key] += 1
    rows = []
    for t in tally.values():
        t["points"] = t["gold"] * gold_pts + t["silver"] * silver_pts + t["copper"] * copper_pts
        rows.append(t)
    rows.sort(key=lambda t: (-t["points"], -t["gold"], -t["silver"], -t["copper"], t["entry"]["number"]))
    previous_key, rank = None, 0
    for position, t in enumerate(rows, start=1):
        key = (t["points"], t["gold"], t["silver"], t["copper"])
        if key != previous_key:
            rank = position
            previous_key = key
        t["rank"] = rank
    return rows


def get_standings(contest_id):
    """Returns (ranked rows, number of ballots cast)."""
    contest = get_contest(contest_id)
    entries = entries_for(contest_id)
    with _db() as conn:
        ballots = [dict(r) for r in conn.execute(
            "SELECT gold_entry_id, silver_entry_id, copper_entry_id FROM ct_ballots WHERE contest_id = ?", (contest_id,)
        ).fetchall()]
    return (rank_entries(entries, ballots, contest["gold_pts"], contest["silver_pts"], contest["copper_pts"]), len(ballots))


def podium(ranked):
    """The entries that placed in the top three. If entries tie, more than three can appear.
    Entries with no points never place."""
    return [r for r in ranked if r["rank"] <= 3 and r["points"] > 0]


# ============================================================
#  SMALL DISCORD HELPERS
# ============================================================

def _is_mod(user):
    return any(r.name in _mod_roles for r in getattr(user, "roles", []))


async def _deny_wrong_guild(interaction):
    if interaction.guild is None or interaction.guild.name not in _guild_names:
        await interaction.response.send_message("⚠️ Contests aren't active in this server.", ephemeral=True)
        return True
    return False


async def _deny_non_mod(interaction):
    if not _is_mod(interaction.user):
        await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
        return True
    return False


def _valid_image_url(url):
    return bool(url) and len(url) <= 500 and url.lower().startswith(("http://", "https://")) and " " not in url


def _owner_name(guild, owner_id):
    member = guild.get_member(owner_id) if guild is not None else None
    return member.display_name if member is not None else "a member"


def _entry_label(entry):
    return f"#{entry['number']} · {entry['pet_name']}"[:100]


def _status_label(status):
    return {"setup": "🛠️ Setting up", "voting": "🗳️ Voting open", "closed": "🏁 Closed", "cancelled": "🚫 Cancelled"}.get(status, status)


async def _resolve_contest(interaction, contest_id, statuses, what):
    """Picks the contest a command is about. If the mod didn't choose one and exactly one
    fits, use it. Sends an explanation and returns None otherwise."""
    if contest_id is not None:
        c = get_contest(contest_id)
        if not c or c["status"] not in statuses:
            await interaction.response.send_message(f"⚠️ That contest isn't {what}.", ephemeral=True)
            return None
        return c
    candidates = list_contests(statuses)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        await interaction.response.send_message(f"⚠️ There's no contest that's {what}.", ephemeral=True)
        return None
    names = ", ".join(f"#{c['id']} {c['name']}" for c in candidates[:8])
    await interaction.response.send_message(
        f"⚠️ More than one contest is {what} ({names}). Choose which one in the `contest` option.", ephemeral=True
    )
    return None


async def _contest_choices(interaction, current, statuses):
    if not _is_mod(interaction.user):
        return []
    out = []
    for c in list_contests(statuses):
        if current.lower() in c["name"].lower():
            out.append(app_commands.Choice(name=f"#{c['id']} {c['name']} ({c['status']})"[:100], value=c["id"]))
    return out[:25]


# ============================================================
#  THE BALLOT (what a voter sees; everything here is private to them)
# ============================================================

def _ballot_intro(contest, required, ends_at, paged=False):
    medals = "\n".join(
        f"{medal} **{name}**: {contest[f'{key}_pts']} points"
        for key, medal, name in TIERS[:required]
    )
    one, many = words(contest)
    how = (f"Use the medal tabs and the ◀ ▶ buttons to browse all the {many}, and pick a different one for each medal."
           if paged else f"Pick a different {one} for each medal.")
    return (
        f"🗳️ **{contest['name']}: your secret ballot**\n"
        f"{how} You can't pick your own entries (they're not in the lists).\n"
        f"{medals}\n\n"
        f"🔒 Nobody can see who you voted for, and your vote is final once you cast it.\n"
        f"Voting ends {_ts(ends_at)} ({_ts(ends_at, 'R')})."
    )


class _BallotBase(discord.ui.View):
    """What the simple ballot and the paged ballot have in common."""

    def _setup_state(self, contest, eligible, required, voter_id, option_descriptions, choices):
        self.contest = contest
        self.eligible = eligible
        self.required = required
        self.voter_id = voter_id
        self.option_descriptions = option_descriptions
        self.choices = dict(choices or {})

    async def _ours(self, interaction):
        if interaction.user.id != self.voter_id:
            await interaction.response.send_message("⚠️ This ballot belongs to someone else.", ephemeral=True)
            return False
        return True

    def problem(self):
        """What's wrong with the current picks, or None if the ballot is ready."""
        keys = [t[0] for t in TIERS][:self.required]
        missing = [t[2] for t in TIERS[:self.required] if t[0] not in self.choices]
        if missing:
            return "⚠️ You still need to choose: " + ", ".join(missing) + "."
        ids = [self.choices[k] for k in keys]
        if len(set(ids)) != len(ids):
            return f"⚠️ Each medal has to go to a different {words(self.contest)[0]}. You picked the same one twice."
        return None

    def review_text(self):
        by_id = {e["id"]: e for e in self.eligible}
        lines = [f"{medal} **{name}** ({self.contest[f'{key}_pts']} pts): {_entry_label(by_id[self.choices[key]])}"
                 for key, medal, name in TIERS[:self.required]]
        return "**Please check your ballot.** This is secret and final.\n\n" + "\n".join(lines)

    async def _do_review(self, interaction):
        if not await self._ours(interaction):
            return
        problem = self.problem()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.edit_message(content=self.review_text(), view=ConfirmBallotView(self))

    async def _do_cancel(self, interaction):
        if not await self._ours(interaction):
            return
        self.stop()
        await interaction.response.edit_message(
            content="Ballot cancelled. Nothing was recorded. You can vote any time before the deadline.", view=None)


class BallotView(_BallotBase):
    """The simple ballot (up to 25 entries): one dropdown per medal."""

    def __init__(self, contest, eligible, required, voter_id, option_descriptions, choices=None):
        super().__init__(timeout=900)
        self._setup_state(contest, eligible, required, voter_id, option_descriptions, choices)
        self.selects = {}
        for key, medal, name in TIERS[:required]:
            pts = contest[f"{key}_pts"]
            options = [
                discord.SelectOption(
                    label=_entry_label(e), value=str(e["id"]),
                    description=(option_descriptions.get(e["id"]) or None),
                    default=(self.choices.get(key) == e["id"]),
                )
                for e in eligible
            ]
            select = discord.ui.Select(
                placeholder=f"{medal} {name} ({pts} points)", min_values=1, max_values=1, options=options
            )
            select.callback = self._make_callback(key, select)
            self.add_item(select)
            self.selects[key] = select

    def _make_callback(self, key, select):
        async def callback(interaction: discord.Interaction):
            await self.choose(interaction, key, int(select.values[0]))
        return callback

    async def choose(self, interaction, key, entry_id):
        if not await self._ours(interaction):
            return
        self.choices[key] = entry_id
        await interaction.response.defer()

    def intro_text(self):
        return _ballot_intro(self.contest, self.required, self.contest["voting_ends_at"])

    def restore_view(self):
        return BallotView(self.contest, self.eligible, self.required, self.voter_id,
                          self.option_descriptions, choices=self.choices)

    @discord.ui.button(label="Review my ballot", style=discord.ButtonStyle.primary, emoji="🗳️", row=3)
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_review(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_cancel(interaction)


class PagedBallotView(_BallotBase):
    """The ballot for bigger contests (more than 25 entries). Discord dropdowns hold 25 choices,
    so this shows one medal at a time (tabs), one page of 25 entries at a time (arrows)."""

    def __init__(self, contest, eligible, required, voter_id, option_descriptions, choices=None):
        super().__init__(timeout=900)
        self._setup_state(contest, eligible, required, voter_id, option_descriptions, choices)
        self.pages = max(1, math.ceil(len(eligible) / OPTIONS_PER_PAGE))
        self.tier = next((t[0] for t in TIERS[:required] if t[0] not in self.choices), TIERS[0][0])
        self.page = 0
        self._focus_chosen_page()
        self._build()

    # ---- state helpers ----
    def _focus_chosen_page(self):
        """Show the page the current medal's pick is on, if there is one."""
        chosen = self.choices.get(self.tier)
        if chosen is not None:
            for index, e in enumerate(self.eligible):
                if e["id"] == chosen:
                    self.page = index // OPTIONS_PER_PAGE
                    return

    def _tier(self, key):
        return next(t for t in TIERS if t[0] == key)

    def _number_of(self, entry_id):
        return next((e["number"] for e in self.eligible if e["id"] == entry_id), None)

    # ---- drawing ----
    def _build(self):
        self.clear_items()
        key, medal, name = self._tier(self.tier)
        pts = self.contest[f"{key}_pts"]
        start = self.page * OPTIONS_PER_PAGE
        used_elsewhere = {eid: k for k, eid in self.choices.items() if k != self.tier}

        options = []
        for e in self.eligible[start:start + OPTIONS_PER_PAGE]:
            label = _entry_label(e)
            if e["id"] in used_elsewhere:           # already holds another medal: mark it
                label = f"{self._tier(used_elsewhere[e['id']])[1]} {label}"[:100]
            options.append(discord.SelectOption(
                label=label, value=str(e["id"]),
                description=(self.option_descriptions.get(e["id"]) or None),
                default=(self.choices.get(self.tier) == e["id"]),
            ))
        select = discord.ui.Select(
            placeholder=f"{medal} Pick your {name} ({pts} pts): page {self.page + 1} of {self.pages}",
            min_values=1, max_values=1, options=options, row=0)

        async def on_pick(interaction: discord.Interaction):
            await self.choose(interaction, self.tier, int(select.values[0]))
        select.callback = on_pick
        self.add_item(select)
        self.select = select

        for k, m, n in TIERS[:self.required]:
            chosen = self.choices.get(k)
            label = f"{m} {n}" + (f": #{self._number_of(chosen)}" if chosen is not None else "")
            style = (discord.ButtonStyle.primary if k == self.tier
                     else discord.ButtonStyle.success if chosen is not None else discord.ButtonStyle.secondary)
            tab = discord.ui.Button(label=label[:80], style=style, row=1)
            tab.callback = self._tab_callback(k)
            self.add_item(tab)

        prev_button = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=2)
        page_label = discord.ui.Button(label=f"Page {self.page + 1} / {self.pages}", style=discord.ButtonStyle.secondary, disabled=True, row=2)
        next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.pages - 1, row=2)

        async def go_prev(interaction: discord.Interaction): await self.turn_page(interaction, -1)
        async def go_next(interaction: discord.Interaction): await self.turn_page(interaction, +1)
        prev_button.callback, next_button.callback = go_prev, go_next
        self.add_item(prev_button); self.add_item(page_label); self.add_item(next_button)
        self.prev_button, self.next_button = prev_button, next_button

        review = discord.ui.Button(label="Review my ballot", style=discord.ButtonStyle.primary, emoji="🗳️", row=3)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)

        async def go_review(interaction: discord.Interaction): await self._do_review(interaction)
        async def go_cancel(interaction: discord.Interaction): await self._do_cancel(interaction)
        review.callback, cancel.callback = go_review, go_cancel
        self.add_item(review); self.add_item(cancel)
        self.review_button, self.cancel_button = review, cancel

    def _tab_callback(self, key):
        async def callback(interaction: discord.Interaction):
            await self.switch_tier(interaction, key)
        return callback

    def render_text(self):
        key, medal, name = self._tier(self.tier)
        lines = []
        for k, m, n in TIERS[:self.required]:
            chosen = self.choices.get(k)
            lines.append(f"{m} {n}: " + (f"#{self._number_of(chosen)}" if chosen is not None else "*not chosen yet*"))
        return (_ballot_intro(self.contest, self.required, self.contest["voting_ends_at"], paged=True) +
                f"\n\n**Choosing your {medal} {name} now.**\n" + " · ".join(lines))

    def intro_text(self):
        return self.render_text()

    def restore_view(self):
        self._build()
        return self

    async def _redraw(self, interaction):
        self._build()
        await interaction.response.edit_message(content=self.render_text(), view=self)

    # ---- what the buttons and the dropdown do ----
    async def choose(self, interaction, key, entry_id):
        if not await self._ours(interaction):
            return
        for other_key, other_id in self.choices.items():
            if other_key != key and other_id == entry_id:
                await interaction.response.send_message(
                    f"⚠️ You already gave #{self._number_of(entry_id)} your {self._tier(other_key)[1]} {self._tier(other_key)[2]} "
                    f"medal. Each medal has to go to a different {words(self.contest)[0]}. Change that one first.", ephemeral=True)
                return
        self.choices[key] = entry_id
        order = [t[0] for t in TIERS[:self.required]]
        later_first = order[order.index(key) + 1:] + order[:order.index(key)]
        self.tier = next((k for k in later_first if k not in self.choices), key)     # move on to the next medal still to pick
        self._focus_chosen_page()
        await self._redraw(interaction)

    async def switch_tier(self, interaction, key):
        if not await self._ours(interaction):
            return
        self.tier = key
        self._focus_chosen_page()
        await self._redraw(interaction)

    async def turn_page(self, interaction, delta):
        if not await self._ours(interaction):
            return
        self.page = max(0, min(self.pages - 1, self.page + delta))
        await self._redraw(interaction)

    async def review(self, interaction):
        await self._do_review(interaction)

    async def cancel(self, interaction):
        await self._do_cancel(interaction)


class ConfirmBallotView(discord.ui.View):
    def __init__(self, ballot):
        super().__init__(timeout=900)
        self.ballot = ballot
        self.done = False

    @discord.ui.button(label="Cast my vote (final)", style=discord.ButtonStyle.success, emoji="✅")
    async def cast(self, interaction: discord.Interaction, button: discord.ui.Button):
        b = self.ballot
        if interaction.user.id != b.voter_id:
            await interaction.response.send_message("⚠️ This ballot belongs to someone else.", ephemeral=True)
            return
        if self.done:
            await interaction.response.send_message("That's already been handled.", ephemeral=True)
            return
        self.done = True
        self.stop()
        try:
            ok, why = cast_ballot(b.contest["id"], b.voter_id, dict(b.choices), _now())
        except Exception as e:
            # Deliberately prints nothing about the member or their picks
            print(f"⚠️ Contest: a ballot couldn't be saved ({type(e).__name__})")
            ok, why = False, "Something went wrong and your ballot was not recorded. Please try again."
        if ok:
            await interaction.response.edit_message(
                content="✅ **Your vote is in.** It's secret and final. Thank you for voting!", view=None)
        else:
            await interaction.response.edit_message(content=f"⚠️ {why}", view=None)

    @discord.ui.button(label="Go back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        b = self.ballot
        if interaction.user.id != b.voter_id:
            await interaction.response.send_message("⚠️ This ballot belongs to someone else.", ephemeral=True)
            return
        self.stop()
        view = b.restore_view()
        await interaction.response.edit_message(content=view.intro_text(), view=view)


async def start_ballot(interaction, contest):
    """Opens a member's private ballot. Used by the Vote button and by /vote."""
    if interaction.guild is None or interaction.guild.name not in _guild_names:
        await interaction.response.send_message("⚠️ Contests aren't active in this server.", ephemeral=True)
        return
    user = interaction.user
    if getattr(user, "bot", False):
        await interaction.response.send_message("⚠️ Bots can't vote.", ephemeral=True)
        return
    if CONTEST_VOTER_ROLES and not any(r.name in CONTEST_VOTER_ROLES for r in getattr(user, "roles", [])):
        await interaction.response.send_message(
            "⚠️ Voting is limited to members with one of these roles: " + ", ".join(CONTEST_VOTER_ROLES), ephemeral=True)
        return
    now = _now()
    if contest["status"] != "voting" or not contest["voting_ends_at"] or now >= _parse(contest["voting_ends_at"]):
        await interaction.response.send_message("⚠️ Voting for this contest has closed.", ephemeral=True)
        return
    if has_voted(contest["id"], user.id):
        await interaction.response.send_message("You've already voted in this contest. Votes are final. Thank you! 🗳️", ephemeral=True)
        return

    eligible = eligible_entries(contest["id"], user.id)
    required = required_picks(len(eligible))
    if required == 0:
        await interaction.response.send_message("⚠️ There's nothing for you to vote on in this contest.", ephemeral=True)
        return
    descriptions = {}
    for e in eligible:
        text = ""
        if contest["show_owners"]:
            text = f"by {_owner_name(interaction.guild, e['owner_id'])}"
        elif e.get("description"):
            text = e["description"]
        descriptions[e["id"]] = text[:100]
    ballot_class = PagedBallotView if len(eligible) > OPTIONS_PER_PAGE else BallotView
    view = ballot_class(contest, eligible, required, user.id, descriptions)
    await interaction.response.send_message(content=view.intro_text(), view=view, ephemeral=True)


class ContestVoteView(discord.ui.View):
    """The persistent Vote button under the contest post. It keeps working after restarts
    because the contest is looked up from the message the button is on."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Vote", style=discord.ButtonStyle.success, emoji="🗳️", custom_id="ct_vote")
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        contest = contest_by_vote_message(interaction.message.id) if interaction.message else None
        if not contest:
            await interaction.response.send_message("⚠️ Voting for this contest isn't open any more.", ephemeral=True)
            return
        await start_ballot(interaction, contest)


# ============================================================
#  POSTS: the entries, the vote button, the results
# ============================================================

def _entry_embed(contest, entry, guild):
    embed = discord.Embed(title=f"#{entry['number']} · {entry['pet_name']}"[:256], color=EMBED_COLOR)
    parts = []
    if entry.get("description"):
        parts.append(entry["description"])
    if entry.get("body"):
        body = entry["body"]
        if len(body) > EMBED_TEXT_CHARS:
            body = body[:EMBED_TEXT_CHARS].rstrip() + "…\n*(the full text is on the web gallery)*"
        parts.append(body)
    if parts:
        embed.description = "\n\n".join(parts)[:4000]
    if contest["show_owners"]:
        embed.add_field(name="Entered by", value=f"<@{entry['owner_id']}>", inline=False)
    if _valid_image_url(entry.get("image_url")):
        embed.set_image(url=entry["image_url"])
    embed.set_footer(text=contest["name"])
    return embed


def _embed_batches(embeds, max_per_message=5, max_chars=5500):
    """Groups embeds into messages. Discord allows at most 10 embeds and about 6000 characters
    of embed text per message, so long entries (poems) get fewer to a message."""
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


def _compact_entry_chunks(contest, entries, header):
    """Entry lists as plain text, split into messages that fit Discord's length limit."""
    lines = []
    for e in entries:
        line = f"**#{e['number']}** {e['pet_name']}"
        if contest["show_owners"]:
            line += f" · by <@{e['owner_id']}>"
        lines.append(line)
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + 1 > 1800:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    return chunks


def _vote_post_embed(contest, ends_at, link=None):
    desc = (
        f"Pick your favorites! Everyone gets three medals:\n"
        f"🥇 Gold: **{contest['gold_pts']}** points\n"
        f"🥈 Silver: **{contest['silver_pts']}** points\n"
        f"🥉 Copper: **{contest['copper_pts']}** points\n\n"
        f"• You **can't** vote for your own entry.\n"
        f"• 🔒 Your ballot is **secret**: nobody can see who you voted for, not even the mods.\n"
        f"• Votes are **final** once cast.\n\n"
        f"**Voting ends:** {_ts(ends_at)} ({_ts(ends_at, 'R')})\n"
        + (f"🖼️ **See every entry with pictures:** {link}\n" if link else "")
        + "Press the button below (or use `/vote`)."
    )
    return discord.Embed(title=f"🗳️ Vote now: {contest['name']}", description=desc, color=EMBED_COLOR)


def results_embed(contest, ranked, ballots):
    placed = podium(ranked)
    embed = discord.Embed(title=f"🏆 Results: {contest['name']}", color=EMBED_COLOR)
    if not placed:
        embed.description = "No votes were cast, so there's no winner this time."
        return embed
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for r in placed:
        e = r["entry"]
        shared = sum(1 for x in placed if x["rank"] == r["rank"]) > 1
        lines.append(
            f"{medals[r['rank']]}{' (tie)' if shared else ''} **#{e['number']} · {e['pet_name']}**, entered by "
            f"<@{e['owner_id']}>: **{r['points']} points** "
            f"({r['gold']}🥇 {r['silver']}🥈 {r['copper']}🥉)"
        )
    embed.description = "\n".join(lines)
    winner_image = next((r["entry"]["image_url"] for r in placed if r["rank"] == 1 and _valid_image_url(r["entry"].get("image_url"))), None)
    if winner_image:
        embed.set_thumbnail(url=winner_image)
    embed.set_footer(text=f"{ballots} member{'s' if ballots != 1 else ''} voted · secret ballot")
    return embed


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


# ============================================================
#  WEB GALLERY: a public page on the website showing every entry
# ============================================================

def web_enabled():
    return bool(WEB_SYNC_URL) and bool(os.environ.get(WEB_SECRET_ENV))


def web_link(contest):
    """The shareable link for a contest, or None if the web gallery is off or this contest opted out."""
    if not web_enabled() or not contest.get("public_page"):
        return None
    return f"{WEB_PUBLIC_URL.rstrip('/')}/?c={contest['id']}"


def build_web_payload(contest):
    """Exactly what gets sent to the website. It holds the entries, the contest's state, and
    (only once the contest is closed) the final top three. It NEVER holds who voted or what
    any individual chose. Owners' names are left out while voting is open if the contest hides owners."""
    guild = discord.utils.get(_bot.guilds, name=_guild_names[0]) if _bot is not None and _guild_names else None
    closed = contest["status"] == "closed"
    reveal_owners = bool(contest["show_owners"]) or closed
    entries = []
    for e in entries_for(contest["id"]):
        owner = None
        if reveal_owners and guild is not None:
            member = guild.get_member(e["owner_id"])
            owner = member.display_name if member is not None else None
        entries.append({
            "number": e["number"],
            "pet_name": e["pet_name"],
            "owner_name": owner,
            "image_url": e["image_url"] if _valid_image_url(e["image_url"]) else None,
            "description": e["description"],
            "body": e["body"],
        })
    payload = {
        "contest": {
            "id": contest["id"],
            "name": contest["name"],
            "status": contest["status"],
            "show_owners": bool(contest["show_owners"]),
            "voting_ends_at": _parse(contest["voting_ends_at"]).isoformat() if contest["voting_ends_at"] else None,
            "closed_at": _parse(contest["closed_at"]).isoformat() if contest["closed_at"] else None,
            "gold_pts": contest["gold_pts"], "silver_pts": contest["silver_pts"], "copper_pts": contest["copper_pts"],
            "ballots": None,
            "kind": contest["kind"], "entry_singular": contest["entry_singular"], "entry_plural": contest["entry_plural"],
        },
        "entries": entries,
        "results": [],
    }
    if closed:
        ranked, ballots = get_standings(contest["id"])
        payload["contest"]["ballots"] = ballots
        payload["results"] = [
            {"rank": r["rank"], "entry_number": r["entry"]["number"], "points": r["points"],
             "gold": r["gold"], "silver": r["silver"], "copper": r["copper"]}
            for r in podium(ranked)
        ]
    return payload


async def push_web(contest_id):
    """Sends a contest to the website. Returns True if the website accepted it.
    Never raises: a website problem must never get in the way of a contest."""
    if not web_enabled():
        return False
    contest = get_contest(contest_id)
    if not contest or contest["status"] == "setup" or not contest["public_page"]:
        return False
    try:
        payload = build_web_payload(contest)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WEB_SYNC_URL, json=payload, headers={"X-Sync-Secret": os.environ.get(WEB_SECRET_ENV, "")},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    result = {}
                ok = resp.status == 200 and isinstance(result, dict) and bool(result.get("success"))
                problem = (result.get("error") if isinstance(result, dict) else None) or f"HTTP {resp.status}"
    except Exception as e:
        print(f"⚠️ Contest: couldn't reach the web gallery ({type(e).__name__})")
        return False
    if not ok:
        print(f"⚠️ Contest: the web gallery rejected '{contest['name']}' ({problem})")
        return False
    with _db() as conn:
        conn.execute("UPDATE ct_contests SET web_synced_status = ? WHERE id = ?", (contest["status"], contest_id))
        conn.commit()
    return True


async def sync_web_if_needed(now):
    """Retries any contest whose website copy is out of date (website down, first run, etc.)."""
    if not web_enabled():
        return
    for c in list_contests(["voting", "closed", "cancelled"]):
        if not c["public_page"] or c["web_synced_status"] == c["status"]:
            continue
        key = (c["id"], c["status"])
        if _web_failures.get(key, 0) >= WEB_MAX_ATTEMPTS:
            continue
        if now < _web_next_try.get(key, now - timedelta(seconds=1)):
            continue
        if await push_web(c["id"]):
            _web_failures.pop(key, None)
            _web_next_try.pop(key, None)
        else:
            _web_failures[key] = _web_failures.get(key, 0) + 1
            _web_next_try[key] = now + timedelta(minutes=WEB_RETRY_MINUTES)


async def finish_contest(contest):
    """Posts the results and switches off the Vote button. Safe to call again: it only
    marks the contest done once the results have really been posted (or after several failed tries)."""
    ranked, ballots = get_standings(contest["id"])
    channel = await _get_channel(contest["announce_channel_id"])
    posted = False
    results_message_id = None
    if channel is None:
        print(f"⚠️ Contest: couldn't find the announcement channel for '{contest['name']}'")
    else:
        try:
            sent = await channel.send(embed=results_embed(contest, ranked, ballots),
                                      allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            results_message_id = getattr(sent, "id", None)
            posted = True
        except Exception as e:
            print(f"⚠️ Contest: couldn't post results for '{contest['name']}' ({type(e).__name__})")

    # switch off the Vote button on the original post
    try:
        vote_channel = await _get_channel(contest["vote_channel_id"])
        if vote_channel is not None and contest["vote_message_id"]:
            msg = await vote_channel.fetch_message(contest["vote_message_id"])
            await msg.edit(embed=discord.Embed(title=f"🔒 Voting closed: {contest['name']}",
                                               description="Thanks to everyone who voted! The results have been posted.",
                                               color=EMBED_COLOR), view=None)
    except Exception:
        pass  # the original message may have been deleted; nothing else to do

    with _db() as conn:
        attempts = contest["results_attempts"] + 1
        done = 1 if (posted or attempts >= MAX_RESULT_ATTEMPTS or channel is None) else 0
        conn.execute("UPDATE ct_contests SET results_posted = ?, results_attempts = ?, "
                     "results_message_id = COALESCE(?, results_message_id) WHERE id = ?",
                     (done, attempts, results_message_id, contest["id"]))
        conn.commit()
    await push_web(contest["id"])        # the web gallery now shows the final results too
    return posted


async def contest_tick(now=None):
    """Closes contests whose voting time is up, and posts any results still waiting."""
    now = now or _now()
    for c in list_contests(["voting"]):
        if c["voting_ends_at"] and now >= _parse(c["voting_ends_at"]):
            ok, _ = close_contest(c["id"], now)
            if ok:
                print(f"🏁 Contest '{c['name']}' voting ended")
    for c in list_contests(["closed"]):
        if not c["results_posted"]:
            try:
                await finish_contest(get_contest(c["id"]))
            except Exception as e:
                print(f"⚠️ Contest: problem finishing '{c['name']}' ({type(e).__name__})")
    try:
        await sync_web_if_needed(now)
    except Exception as e:
        print(f"⚠️ Contest: web gallery sync problem ({type(e).__name__})")


async def apply_extension(contest_id, info, reason=None, announce=True):
    """After extend_contest() has moved the deadline: refresh the vote post, void old results if the
    contest was reopened, tell the channel, and update the web gallery. Nothing here can fail the extension."""
    c = get_contest(contest_id)
    reopened = info["was"] == "closed"
    link = web_link(c)

    # 1) the vote post shows the new deadline (and gets its button back if the contest had closed)
    try:
        vote_channel = await _get_channel(c["vote_channel_id"])
        if vote_channel is not None and c["vote_message_id"]:
            msg = await vote_channel.fetch_message(c["vote_message_id"])
            await msg.edit(embed=_vote_post_embed(c, c["voting_ends_at"], link), view=ContestVoteView())
    except Exception as e:
        print(f"⚠️ Contest: couldn't refresh the vote post ({type(e).__name__})")

    # 2) results that were already posted no longer stand
    if reopened and info.get("results_message_id"):
        try:
            results_channel = await _get_channel(c["announce_channel_id"])
            old = await results_channel.fetch_message(info["results_message_id"])
            await old.edit(embed=discord.Embed(
                title=f"⚠️ Void: results for {c['name']}",
                description="Voting was reopened, so these results no longer stand. New results will be posted when voting ends.",
                color=EMBED_COLOR))
        except Exception as e:
            print(f"⚠️ Contest: couldn't mark the old results as void ({type(e).__name__})")

    # 3) tell the channel
    if announce:
        channel = await _get_channel(c["announce_channel_id"])
        if channel is not None:
            if reopened:
                title = f"🔓 Voting has reopened: {c['name']}"
                body = (f"Voting now ends {_ts(c['voting_ends_at'])} ({_ts(c['voting_ends_at'], 'R')}). "
                        f"The earlier results are void. Ballots already cast still count, and nobody can vote twice.")
            else:
                title = f"⏰ More time to vote: {c['name']}"
                body = f"Voting now ends {_ts(c['voting_ends_at'])} ({_ts(c['voting_ends_at'], 'R')})."
            if reason:
                body += f"\n**Why:** {reason}"
            body += "\nHaven't voted yet? Press the Vote button on the entries post, or use `/vote`."
            try:
                await channel.send(embed=discord.Embed(title=title, description=body, color=EMBED_COLOR))
            except Exception as e:
                print(f"⚠️ Contest: couldn't post the extension notice ({type(e).__name__})")

    # 4) the web gallery shows the new deadline (and drops any results)
    await push_web(contest_id)


async def contest_loop():
    await _bot.wait_until_ready()
    while not _bot.is_closed():
        try:
            await contest_tick()
        except Exception as e:
            print(f"⚠️ Contest: loop error ({type(e).__name__})")
        await asyncio.sleep(CONTEST_LOOP_SECONDS)


# ============================================================
#  MEMBER COMMAND: /vote
# ============================================================

@app_commands.command(name="vote", description="Cast your secret ballot in a contest")
@app_commands.describe(contest="Which contest (only needed if more than one is open)")
@app_commands.guild_only()
async def vote_cmd(interaction: discord.Interaction, contest: Optional[int] = None):
    if await _deny_wrong_guild(interaction):
        return
    if contest is not None:
        c = get_contest(contest)
    else:
        open_ones = list_contests(["voting"])
        if not open_ones:
            await interaction.response.send_message("There's no contest taking votes right now.", ephemeral=True)
            return
        if len(open_ones) > 1:
            await interaction.response.send_message(
                "More than one contest is open. Pick one in the `contest` option: " +
                ", ".join(f"#{x['id']} {x['name']}" for x in open_ones[:8]), ephemeral=True)
            return
        c = open_ones[0]
    if not c or c["status"] != "voting":
        await interaction.response.send_message("⚠️ That contest isn't taking votes.", ephemeral=True)
        return
    await start_ballot(interaction, c)


@vote_cmd.autocomplete("contest")
async def _ac_vote_contest(interaction: discord.Interaction, current: str):
    out = []
    for c in list_contests(["voting"]):
        if current.lower() in c["name"].lower():
            out.append(app_commands.Choice(name=f"#{c['id']} {c['name']}"[:100], value=c["id"]))
    return out[:25]


# ============================================================
#  MOD COMMANDS: /contest
# ============================================================

contest_group = app_commands.Group(name="contest", description="[Mod] Run a voting contest", guild_only=True)


def _kind_choices():
    return [app_commands.Choice(name=v["label"], value=k) for k, v in KINDS.items()]


@contest_group.command(name="create", description="[Mod] Start setting up a new contest")
@app_commands.describe(
    name="What the contest is called",
    kind="What kind of contest it is (sets the wording, and whether entries are text like poems)",
    announce_channel="Where the entries and results are posted (default: this channel)",
    show_owners="Show who entered each entry while people vote? (default yes)",
    public_page="Also publish a shareable web gallery of the entries? (default yes)",
    entry_word="Your own word for an entry, like 'recipe' (only if the kinds above don't fit)",
    entry_plural="The plural of that word (guessed automatically if you leave it out)",
    entry_text="Make entries text, like poems? (only needed to override the kind)",
)
@app_commands.choices(kind=_kind_choices())
async def contest_create(interaction: discord.Interaction, name: app_commands.Range[str, 1, 80],
                         kind: app_commands.Choice[str],
                         announce_channel: Optional[discord.TextChannel] = None, show_owners: bool = True,
                         public_page: bool = True, entry_word: Optional[app_commands.Range[str, 1, 30]] = None,
                         entry_plural: Optional[app_commands.Range[str, 1, 30]] = None,
                         entry_text: Optional[bool] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    channel = announce_channel or interaction.channel
    cid = create_contest(name.strip(), interaction.user.id, getattr(channel, "id", None), show_owners, _now(),
                         public_page, kind.value, entry_word, entry_plural, entry_text)
    c = get_contest(cid)
    one, many = words(c)
    gallery_note = ""
    if public_page and web_enabled():
        gallery_note = "\n🖼️ A shareable web gallery will be published when you open voting."
    elif public_page:
        gallery_note = "\n(The web gallery isn't set up on this bot, so entries will be posted in the channel only.)"
    text_note = f"\n📝 Each {one} is made of text, so you'll paste it into a pop-up form when you add it." if c["text_entries"] else ""
    await interaction.response.send_message(
        f"✅ **{name.strip()}** created (#{cid}), a {KINDS.get(c['kind'], KINDS['other'])['label'].lower()}. "
        f"Entries are called \"{one}\" / \"{many}\". Entries and results will be posted in "
        f"{getattr(channel, 'mention', 'this channel')}.{gallery_note}{text_note}\n"
        f"Next: add each {one} with `/contest addentry`, then start voting with `/contest open`.",
        ephemeral=True
    )


class EntryTextModal(discord.ui.Modal):
    """The pop-up where a mod pastes an entry's text (a poem). Used both when adding a text entry and
    when fixing one later. A pop-up is used because a slash-command box can't hold several lines."""

    def __init__(self, contest, title, entrant=None, image_url=None, description=None, number=None, default=""):
        one, _many = words(contest)
        super().__init__(title=(f"Paste the {one}'s text" if number is None else f"Edit the {one}'s text")[:45])
        self.contest_id = contest["id"]
        self.entry_title, self.entrant, self.image_url, self.description, self.number = title, entrant, image_url, description, number
        self.text = discord.ui.TextInput(
            label=f"The {one}'s text"[:45], style=discord.TextStyle.paragraph, required=True,
            max_length=MAX_BODY_CHARS, default=(default or None), placeholder="Paste it here, line breaks and all")
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_mod(interaction.user):
            await interaction.response.send_message("⚠️ You don't have permission to use this.", ephemeral=True)
            return
        if self.number is None:
            ok, result = add_entry(self.contest_id, self.entry_title, self.entrant.id, self.image_url, self.description,
                                   body=self.text.value)
            if not ok:
                await interaction.response.send_message(f"⚠️ {result}", ephemeral=True)
                return
            total = len(entries_for(self.contest_id))
            await interaction.response.send_message(
                f"✅ Added **#{result} · {self.entry_title.strip()}** (entered by {self.entrant.display_name}) with its text. "
                f"That's {total} entr{'y' if total == 1 else 'ies'} so far.", ephemeral=True)
        else:
            ok, result = set_entry_body(self.contest_id, self.number, self.text.value)
            await interaction.response.send_message(
                f"✅ Updated the text of #{self.number}." if ok else f"⚠️ {result}", ephemeral=True)


@contest_group.command(name="addentry", description="[Mod] Add an entry and the member who entered it")
@app_commands.describe(
    title="The entry's name or title (the pet's name, the artwork's title, the poem's title)",
    entrant="The member who entered it",
    contest="Which contest (default: the one being set up)",
    image_url="Link to a picture (a link that doesn't expire; Discord attachments do)",
    description="Optional short note shown with the entry, like costume details or the artist's note",
)
async def contest_addentry(interaction: discord.Interaction, title: app_commands.Range[str, 1, 80],
                           entrant: discord.Member, contest: Optional[int] = None,
                           image_url: Optional[str] = None,
                           description: Optional[app_commands.Range[str, 0, 200]] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("setup",), "being set up")
    if c is None:
        return
    if entrant.bot:
        await interaction.response.send_message("⚠️ A bot can't enter the contest.", ephemeral=True)
        return
    image_url = (image_url or "").strip()
    if image_url and not _valid_image_url(image_url):
        await interaction.response.send_message("⚠️ The image link has to be a full web address starting with http:// or https://.", ephemeral=True)
        return
    note = (description or "").strip() or None
    if c["text_entries"]:
        # text entries (poems): collect the text in a pop-up, and add the entry when it's submitted
        await interaction.response.send_modal(EntryTextModal(c, title, entrant, image_url or None, note))
        return
    ok, result = add_entry(c["id"], title, entrant.id, image_url or None, note)
    if not ok:
        await interaction.response.send_message(f"⚠️ {result}", ephemeral=True)
        return
    total = len(entries_for(c["id"]))
    await interaction.response.send_message(
        f"✅ Added **#{result} · {title.strip()}** (entered by {entrant.display_name}) to **{c['name']}**. "
        f"That's {total} entr{'y' if total == 1 else 'ies'} so far.", ephemeral=True
    )


@contest_group.command(name="edittext", description="[Mod] Fix the text of a text entry (like a poem) before voting opens")
@app_commands.describe(number="The entry's number", contest="Which contest (default: the one being set up)")
async def contest_edittext(interaction: discord.Interaction, number: int, contest: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("setup",), "being set up")
    if c is None:
        return
    if not c["text_entries"]:
        await interaction.response.send_message("⚠️ This contest's entries don't have text.", ephemeral=True)
        return
    entry = next((e for e in entries_for(c["id"]) if e["number"] == number), None)
    if entry is None:
        await interaction.response.send_message(f"⚠️ There's no entry #{number} in that contest.", ephemeral=True)
        return
    await interaction.response.send_modal(EntryTextModal(c, entry["pet_name"], number=number, default=entry.get("body") or ""))


@contest_group.command(name="removeentry", description="[Mod] Remove an entry before voting opens")
@app_commands.describe(number="The entry's number", contest="Which contest (default: the one being set up)")
async def contest_removeentry(interaction: discord.Interaction, number: int, contest: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("setup",), "being set up")
    if c is None:
        return
    ok, why = remove_entry(c["id"], number)
    await interaction.response.send_message(f"✅ Removed entry #{number}." if ok else f"⚠️ {why}", ephemeral=True)


@contest_group.command(name="entries", description="[Mod] List a contest's entries")
@app_commands.describe(contest="Which contest (default: the current one)")
async def contest_entries(interaction: discord.Interaction, contest: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("setup", "voting", "closed"), "running")
    if c is None:
        return
    entries = entries_for(c["id"])
    lines = [f"`#{e['number']}` **{e['pet_name']}** by <@{e['owner_id']}>" + (" 🖼️" if e.get("image_url") else "") + (" 📝" if e.get("body") else "") for e in entries]
    await interaction.response.send_message(
        f"**{c['name']}** ({_status_label(c['status'])}): {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}\n" +
        ("\n".join(lines) if lines else "*none yet*"), ephemeral=True)


@contest_group.command(name="open", description="[Mod] Post the entries and open voting")
@app_commands.describe(
    contest="Which contest (default: the one being set up)",
    hours=f"How many hours voting stays open (default {DEFAULT_VOTING_HOURS})",
    ends_on="Or an end date as YYYY-MM-DD (voting ends at 11:59 PM Pacific that day)",
)
async def contest_open(interaction: discord.Interaction, contest: Optional[int] = None,
                       hours: app_commands.Range[int, 1, 720] = DEFAULT_VOTING_HOURS,
                       ends_on: Optional[str] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("setup",), "being set up")
    if c is None:
        return
    entries = entries_for(c["id"])
    if len(entries) < 2:
        await interaction.response.send_message("⚠️ Add at least 2 entries first (`/contest addentry`).", ephemeral=True)
        return

    now = _now()
    if ends_on:
        try:
            ends_at = datetime.strptime(ends_on.strip(), "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=PACIFIC).astimezone(timezone.utc)
        except ValueError:
            await interaction.response.send_message("⚠️ The end date should look like `2026-11-20`.", ephemeral=True)
            return
        if ends_at <= now + timedelta(minutes=5):
            await interaction.response.send_message("⚠️ That end date is too soon or already past.", ephemeral=True)
            return
    else:
        ends_at = now + timedelta(hours=hours)

    channel = await _get_channel(c["announce_channel_id"]) or interaction.channel
    link = web_link(c)
    await interaction.response.defer(ephemeral=True)       # posting several messages takes a moment
    try:
        if len(entries) > GALLERY_EMBED_LIMIT and link:
            # a big contest: one picture card per pet would flood the channel, so post a compact
            # list and point to the web gallery, which shows every picture on one page
            one, many = words(c)
            header = f"📋 **{c['name']}**: {len(entries)} {many}. 🖼️ See every {one} here: {link}\n"
            for chunk in _compact_entry_chunks(c, entries, header):
                await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        else:
            for batch in _embed_batches([_entry_embed(c, entry, interaction.guild) for entry in entries]):
                await channel.send(embeds=batch)
        message = await channel.send(embed=_vote_post_embed(c, _iso(ends_at), link), view=ContestVoteView())
    except Exception as e:
        print(f"⚠️ Contest: couldn't post the entries for '{c['name']}' ({type(e).__name__})")
        await interaction.followup.send(
            "⚠️ I couldn't post in the announcement channel (check that I can send messages and embeds there). "
            "Voting was **not** opened. Fix the permissions and run `/contest open` again.", ephemeral=True)
        return

    ok, why = open_contest(c["id"], ends_at, channel.id, message.id, now)
    if not ok:
        await interaction.followup.send(f"⚠️ {why}", ephemeral=True)
        return
    await push_web(c["id"])             # publish the web gallery right away (it retries by itself if the website is down)
    await interaction.followup.send(
        f"✅ Voting is open for **{c['name']}** and ends {_ts(_iso(ends_at))}. The bot will close it and post the top three automatically."
        + (f"\n🖼️ Shareable gallery: {link}" if link else ""),
        ephemeral=True)


class _ConfirmView(discord.ui.View):
    """A small Confirm / Cancel step for actions that can't be undone."""

    def __init__(self, user_id, action, done_text):
        super().__init__(timeout=120)
        self.user_id, self.action, self.done_text, self.used = user_id, action, done_text, False

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
        text = await self.action()
        await interaction.edit_original_response(content=text)

    @discord.ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⚠️ This belongs to the mod who opened it.", ephemeral=True)
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Okay, nothing changed.", view=None)


@contest_group.command(name="extend", description="[Mod] Give a contest more voting time (or reopen one that has closed)")
@app_commands.describe(
    contest="Which contest (default: the one taking votes). To reopen a finished one, choose it here",
    hours="Add this many hours to the deadline (a reopened contest counts from now)",
    until="Or set a new end date as YYYY-MM-DD (voting ends at 11:59 PM Pacific that day)",
    reason="Optional note shown in the announcement (like 'Discord was down')",
    announce="Post a notice in the channel? (default yes)",
)
async def contest_extend(interaction: discord.Interaction, contest: Optional[int] = None,
                         hours: Optional[app_commands.Range[int, 1, 720]] = None, until: Optional[str] = None,
                         reason: Optional[app_commands.Range[str, 0, 200]] = None, announce: bool = True):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    if (hours is None) == (until is None):
        await interaction.response.send_message("⚠️ Give either `hours` (how much time to add) or `until` (a new end date), not both and not neither.", ephemeral=True)
        return
    if contest is None:
        taking_votes = list_contests(["voting"])
        if not taking_votes:
            await interaction.response.send_message(
                "⚠️ No contest is taking votes right now. To reopen one that finished, pick it in the `contest` option.", ephemeral=True)
            return
        if len(taking_votes) > 1:
            names = ", ".join(f"#{x['id']} {x['name']}" for x in taking_votes[:8])
            await interaction.response.send_message(f"⚠️ More than one contest is taking votes ({names}). Choose which one in the `contest` option.", ephemeral=True)
            return
        c = taking_votes[0]
    else:
        c = get_contest(contest)
        if not c or c["status"] not in ("voting", "closed"):
            await interaction.response.send_message("⚠️ That contest isn't taking votes and hasn't closed, so it can't be extended.", ephemeral=True)
            return

    now = _now()
    if until:
        try:
            new_end = datetime.strptime(until.strip(), "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=PACIFIC).astimezone(timezone.utc)
        except ValueError:
            await interaction.response.send_message("⚠️ The end date should look like `2026-11-20`.", ephemeral=True)
            return
    else:
        base = max(_parse(c["voting_ends_at"]), now) if c["status"] == "voting" else now
        new_end = base + timedelta(hours=hours)
    reason = (reason or "").strip() or None
    contest_id = c["id"]

    async def do_it():
        ok, result = extend_contest(contest_id, new_end, _now())
        if not ok:
            return f"⚠️ {result}"
        await apply_extension(contest_id, result, reason, announce)
        updated = get_contest(contest_id)
        return (f"{'🔓 Reopened' if result['was'] == 'closed' else '⏰ Extended'}: **{updated['name']}** now ends "
                f"{_ts(updated['voting_ends_at'])} ({_ts(updated['voting_ends_at'], 'R')}).")

    if c["status"] == "closed":
        n = ballots_cast(contest_id)
        await interaction.response.send_message(
            f"Reopen voting for **{c['name']}**?\n"
            f"Its results have **already been posted**, so people have seen the standings and later votes could be influenced. "
            f"Reopening marks that results post as void and keeps the **{n}** ballot{'s' if n != 1 else ''} already cast "
            f"(nobody can vote twice). Voting would end {_ts(_iso(new_end))}, and new results are posted then.",
            view=_ConfirmView(interaction.user.id, do_it, ""), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)           # refreshing the posts takes a moment
    await interaction.followup.send(await do_it(), ephemeral=True)


@contest_extend.autocomplete("contest")
async def _ac_extend(interaction: discord.Interaction, current: str):
    return await _contest_choices(interaction, current, ("voting", "closed"))


@contest_group.command(name="close", description="[Mod] End voting now and post the results")
@app_commands.describe(contest="Which contest (default: the one taking votes)")
async def contest_close(interaction: discord.Interaction, contest: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("voting",), "taking votes")
    if c is None:
        return
    n = ballots_cast(c["id"])

    async def do_close():
        ok, why = close_contest(c["id"], _now())
        if not ok:
            return f"⚠️ {why}"
        await finish_contest(get_contest(c["id"]))
        return f"🏁 Voting for **{c['name']}** is closed and the results have been posted."

    await interaction.response.send_message(
        f"End voting for **{c['name']}** right now? {n} ballot{'s' if n != 1 else ''} have been cast so far. "
        f"This posts the results and can't be undone.",
        view=_ConfirmView(interaction.user.id, do_close, ""), ephemeral=True)


@contest_group.command(name="cancel", description="[Mod] Cancel a contest (no results are posted)")
@app_commands.describe(contest="Which contest")
async def contest_cancel(interaction: discord.Interaction, contest: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("setup", "voting"), "running")
    if c is None:
        return

    async def do_cancel():
        ok, why = cancel_contest(c["id"], _now())
        if not ok:
            return f"⚠️ {why}"
        await push_web(c["id"])         # the web gallery will say it was cancelled
        try:
            vote_channel = await _get_channel(c["vote_channel_id"])
            if vote_channel is not None and c["vote_message_id"]:
                msg = await vote_channel.fetch_message(c["vote_message_id"])
                await msg.edit(embed=discord.Embed(title=f"🚫 Cancelled: {c['name']}", description="This contest was cancelled.", color=EMBED_COLOR), view=None)
        except Exception:
            pass
        return f"🚫 **{c['name']}** was cancelled."

    await interaction.response.send_message(
        f"Cancel **{c['name']}**? Any votes already cast will be discarded and no results will be posted.",
        view=_ConfirmView(interaction.user.id, do_cancel, ""), ephemeral=True)


@contest_group.command(name="status", description="[Mod] See how a contest is going")
@app_commands.describe(contest="Which contest (default: the current one)")
async def contest_status(interaction: discord.Interaction, contest: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("setup", "voting", "closed"), "running")
    if c is None:
        return
    text = (f"**{c['name']}**: {_status_label(c['status'])} · {KINDS.get(c['kind'], KINDS['other'])['label']}\n"
            f"Entries: {len(entries_for(c['id']))}\n")
    if c["status"] in ("voting", "closed"):
        text += f"Ballots cast: {ballots_cast(c['id'])}\n"
    link = web_link(c)
    if link and c["status"] in ("voting", "closed"):
        text += f"Shareable gallery: {link}\n"
    if c["status"] == "voting":
        text += f"Voting ends {_ts(c['voting_ends_at'])} ({_ts(c['voting_ends_at'], 'R')})\n"
        text += "*(Standings stay hidden until voting ends, so nobody is influenced.)*"
    await interaction.response.send_message(text, ephemeral=True)


@contest_group.command(name="standings", description="[Mod] Full results for a finished contest")
@app_commands.describe(contest="Which contest (default: the most recent finished one)")
async def contest_standings(interaction: discord.Interaction, contest: Optional[int] = None):
    if await _deny_wrong_guild(interaction) or await _deny_non_mod(interaction):
        return
    c = await _resolve_contest(interaction, contest, ("closed",), "finished")
    if c is None:
        return
    ranked, ballots = get_standings(c["id"])
    lines = [f"**{r['rank']}.** #{r['entry']['number']} {r['entry']['pet_name']}: **{r['points']}** pts "
             f"({r['gold']}🥇 {r['silver']}🥈 {r['copper']}🥉)" for r in ranked]
    await interaction.response.send_message(
        f"**{c['name']}**: full standings ({ballots} ballot{'s' if ballots != 1 else ''})\n" + "\n".join(lines), ephemeral=True)


@contest_open.autocomplete("contest")
async def _ac_open(interaction: discord.Interaction, current: str):
    return await _contest_choices(interaction, current, ("setup",))


@contest_addentry.autocomplete("contest")
async def _ac_addentry(interaction: discord.Interaction, current: str):
    return await _contest_choices(interaction, current, ("setup",))


@contest_removeentry.autocomplete("contest")
async def _ac_removeentry(interaction: discord.Interaction, current: str):
    return await _contest_choices(interaction, current, ("setup",))


@contest_edittext.autocomplete("contest")
async def _ac_edittext(interaction: discord.Interaction, current: str):
    return await _contest_choices(interaction, current, ("setup",))


@contest_close.autocomplete("contest")
async def _ac_close(interaction: discord.Interaction, current: str):
    return await _contest_choices(interaction, current, ("voting",))


@contest_cancel.autocomplete("contest")
async def _ac_cancel(interaction: discord.Interaction, current: str):
    return await _contest_choices(interaction, current, ("setup", "voting"))


@contest_standings.autocomplete("contest")
async def _ac_standings(interaction: discord.Interaction, current: str):
    return await _contest_choices(interaction, current, ("closed",))


# ============================================================
#  SETUP - called from main.py
# ============================================================

def setup(bot, get_db, mod_roles, main_guild_name):
    global _bot, _get_db_fn, _mod_roles, _guild_names
    _bot = bot
    _get_db_fn = get_db
    _mod_roles = list(mod_roles)
    _guild_names = [main_guild_name] + list(CONTEST_GUILD_NAMES_EXTRA)
    init_tables()
    bot.tree.add_command(vote_cmd)
    bot.tree.add_command(contest_group)
    print("🏆 Contest voting ready")


def start_background(bot):
    """Call from on_ready: re-attaches the Vote button on contest posts that were made before
    a restart, and starts the loop that ends voting on time."""
    bot.add_view(ContestVoteView())
    bot.loop.create_task(contest_loop())
