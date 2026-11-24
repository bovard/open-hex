"""Matchmaking — queue management and game creation (Firestore-backed)."""

from __future__ import annotations

import random
from datetime import datetime, timezone

from firebase_admin import firestore

import config as cfg
from services.game_manager import create_game
from services.rankings import PRIOR_SE

# A bot's wait counts from one matchmaking pass (a minute) before it joined, so
# the uncertainty of a bot that joined just before a pass still counts
WAIT_FLOOR_SECONDS = 60.0
# Randomness added to 1v1 ratings when the seed's opponent is chosen, so the
# same bots are not always paired. (A 3p or 4p table changes with its seed; the
# randomness there could slip the top bot into the bottom bots' table.)
RATING_JITTER = 50.0


def _get_db():
    return firestore.client()


def run_matchmaking() -> list[str]:
    """Run one matchmaking pass. Returns list of created game IDs."""
    game_ids = []
    for mode, seats in cfg.MODES.items():
        game_ids.extend(_match_mode(mode, seats))
    return game_ids


def _seconds_waited(joined_at, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(str(joined_at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max((now - t).total_seconds(), 0.0)


def _closest_run(seed: str, others: list[str], size: int, rating: dict) -> list[str]:
    """The size - 1 of `others` that make the table with `seed` of the
    smallest rating spread: a run of bots adjacent in rating that holds the
    seed. (Its nearest bots, taken one by one, could reach both ways and seat
    the top and bottom of the ratings together.)"""
    ranked = sorted([seed] + others, key=lambda b: rating[b])
    i = ranked.index(seed)
    runs = [ranked[j:j + size] for j in range(max(0, i - size + 1), min(i, len(ranked) - size) + 1)]
    best = min(runs, key=lambda run: rating[run[-1]] - rating[run[0]])
    return [b for b in best if b != seed]


def _match_mode(mode: str, players_needed: int, now: datetime | None = None) -> list[str]:
    """Match players for a given mode. Returns created game IDs.

    Each game is built around a seed: the waiting bot with the highest
    uncertainty times wait, its rating's standard error (PRIOR_SE before its
    first rating) times its seconds in the queue (plus WAIT_FLOOR_SECONDS).
    The seed is joined by the waiting bots that, with it, make the table of
    the smallest rating spread, so the table is as evenly matched as the
    queue allows: a bot rated far below the rest spoils a strong table. In
    1v1 that is the bot nearest the seed (ratings with some randomness), and
    not the bot it just played if another is waiting. Uncertain bots are seeded
    sooner, so their ratings settle, and any bot is seeded once it has
    waited long enough. Bots in the middle of the ratings fit most tables
    and play more often than those at either end."""
    db = _get_db()
    now = now or datetime.now(timezone.utc)
    elo_key = f"elo{mode}"

    # Queued bots for this mode, longest waiting first
    queue_docs = list(
        db.collection("queue")
        .where("mode", "==", mode)
        .order_by("joinedAt")
        .stream()
    )

    if len(queue_docs) < players_needed:
        return []

    # A bot already in an active game (one it has left does not count) is not
    # waiting: drop its stale queue entry
    waiting = []
    queue_id = {}
    joined_at = {}
    for qdoc in queue_docs:
        entry = qdoc.to_dict()
        bid = entry["botId"]
        if bid in queue_id:
            continue
        active = [
            g for g in db.collection("games")
            .where("status", "==", "active")
            .where("playerBotIds", "array_contains", bid)
            .get()
            if bid not in (g.to_dict().get("leftBotIds") or [])
        ]
        if active:
            db.collection("queue").document(qdoc.id).delete()
        else:
            waiting.append(bid)
            queue_id[bid] = qdoc.id
            joined_at[bid] = entry.get("joinedAt")

    # Each bot's priority to seed a game, its rating and, for 1v1, its last
    # opponent
    priority = {}
    rating = {}
    last_opp = {}
    for bid in waiting:
        bot_doc = db.collection("bots").document(bid).get()
        bdata = bot_doc.to_dict() if bot_doc.exists else {}
        uncertainty = bdata.get(f"eloSe{mode}") or PRIOR_SE
        waited = _seconds_waited(joined_at[bid], now) + WAIT_FLOOR_SECONDS
        priority[bid] = uncertainty * waited
        rating[bid] = bdata.get(elo_key) or 1500.0
        if mode == "1v1":
            rating[bid] += random.gauss(0, RATING_JITTER)
        last_opp[bid] = bdata.get("lastOpponentId")

    game_ids = []
    while len(waiting) >= players_needed:
        # max() keeps the first of equals: on a tie, the longest waiting
        seed = max(waiting, key=lambda b: priority[b])
        others = [b for b in waiting if b != seed]
        if mode == "1v1" and len(others) > 1:
            # Not the bot it just played (and that just played it)
            others = [b for b in others
                      if not (last_opp.get(seed) == b and last_opp.get(b) == seed)]
        group = [seed] + _closest_run(seed, others, players_needed, rating)

        game_ids.append(create_game(mode, group))
        for bid in group:
            db.collection("queue").document(queue_id[bid]).delete()
        waiting = [b for b in waiting if b not in group]

    return game_ids
