"""Matchmaking — queue management and game creation (Firestore-backed).

A pass builds a mode's games one at a time, in two steps:

1. Pick the seed: the waiting bot with the highest seed priority
   (`seed_priority`). The priority decides only which bot a game is built
   around.
2. Fill its table: the waiting bots that make the table of the smallest
   rating spread with the seed (`_closest_run`), whatever their own priority.
   In 1v1 that is the bot nearest the seed's rating.

The seed is recorded on its bot document (`lastSeededAt`), and the pass goes
on until too few bots are left waiting.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from firebase_admin import firestore

import config as cfg
from services.game_manager import create_game
from services.rankings import PRIOR_SE

# Seed priority, in points; a point is worth ten minutes of waiting
SETTLED_SE = 50.0              # about a settled bot's rating standard error
UNCERTAINTY_POINTS = 8.0       # per SETTLED_SE of standard error above that
IDLE_MINUTES_PER_POINT = 10.0  # since its last game
SEED_MINUTES_PER_POINT = 30.0  # since it last seeded a game
SEED_MINUTES_CAP = 180.0       # never seeded counts as this
TOP_POINTS = 10.0              # at the top of the leaderboard
TOP_HALVING = 400.0            # rating points below the top that halve them
GUARD_MINUTES = 30.0           # a bot this long without a game seeds first
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


def _minutes_since(at, now: datetime) -> float | None:
    if not at:
        return None
    try:
        t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max((now - t).total_seconds() / 60, 0.0)


def seed_priority(se: float, idle_minutes: float, seed_minutes: float | None,
                  top_gap: float) -> tuple[int, float]:
    """A waiting bot's priority to seed a game; the highest seeds.

    `se` is its rating's standard error, `idle_minutes` the time since its
    last game ended (or since it joined the queue, if later or it has never
    played), `seed_minutes` the time since it last seeded a game (None if
    never) and `top_gap` how far its rating is below the top of the
    leaderboard. A bot idle for GUARD_MINUTES or more seeds first, the
    longest idle first. Otherwise its points are the sum of

      uncertainty  8 per 50 of standard error above 50 (a new bot, about 246:
                   about 31)
      idle         1 per 10 minutes since its last game
      unseeded     1 per 30 minutes since it last seeded, up to 6
      top          10 at the top of the leaderboard, halving every 400 below
    """
    if idle_minutes >= GUARD_MINUTES:
        return (1, idle_minutes)
    uncertainty = UNCERTAINTY_POINTS * max(se / SETTLED_SE - 1, 0.0)
    idle = idle_minutes / IDLE_MINUTES_PER_POINT
    unseeded = min(SEED_MINUTES_CAP if seed_minutes is None else seed_minutes,
                   SEED_MINUTES_CAP) / SEED_MINUTES_PER_POINT
    top = TOP_POINTS * 2 ** (-max(top_gap, 0.0) / TOP_HALVING)
    return (0, uncertainty + idle + unseeded + top)


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
    """Match players for a given mode (see the module docstring). Returns
    created game IDs.

    Uncertain bots seed sooner, so their ratings settle; bots near the top of
    the leaderboard seed more often, so the strongest tables are played most;
    no bot goes much more than GUARD_MINUTES without a game. Bots in the
    middle of the ratings fit most tables and play more than those at either
    end, which play mostly the games they seed."""
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

    bots = {}
    for bid in waiting:
        bot_doc = db.collection("bots").document(bid).get()
        bots[bid] = bot_doc.to_dict() if bot_doc.exists else None
    rating = {bid: (d or {}).get(elo_key) or 1500.0 for bid, d in bots.items()}

    # The top of the leaderboard: the rank 1 bot's rating (the best waiting
    # rating while no bot is ranked)
    leader = list(db.collection("bots").where(f"rank{mode}", "==", 1).limit(1).get())
    top = (leader[0].to_dict().get(elo_key) if leader else None) or max(rating.values())

    priority = {}
    for bid in waiting:
        d = bots[bid] or {}
        idle = max(m for m in (_minutes_since(d.get("lastGameAt"), now),
                               _minutes_since(joined_at[bid], now), 0.0) if m is not None)
        priority[bid] = seed_priority(
            se=d.get(f"eloSe{mode}") or PRIOR_SE,
            idle_minutes=idle,
            seed_minutes=_minutes_since(d.get("lastSeededAt"), now),
            top_gap=top - rating[bid])

    # 1v1 opponents are chosen with some randomness in the ratings, and not
    # the bot the seed just played (and that just played it) if another waits
    if mode == "1v1":
        rating = {bid: r + random.gauss(0, RATING_JITTER) for bid, r in rating.items()}
    last_opp = {bid: (d or {}).get("lastOpponentId") for bid, d in bots.items()}

    game_ids = []
    while len(waiting) >= players_needed:
        # max() keeps the first of equals: on a tie, the longest in the queue
        seed = max(waiting, key=lambda b: priority[b])
        others = [b for b in waiting if b != seed]
        if mode == "1v1" and len(others) > 1:
            others = [b for b in others
                      if not (last_opp.get(seed) == b and last_opp.get(b) == seed)]
        group = [seed] + _closest_run(seed, others, players_needed, rating)

        game_ids.append(create_game(mode, group))
        if bots[seed] is not None:
            db.collection("bots").document(seed).update({"lastSeededAt": now.isoformat()})
        for bid in group:
            db.collection("queue").document(queue_id[bid]).delete()
        waiting = [b for b in waiting if b not in group]

    return game_ids
