"""Matchmaking — queue management and game creation (Firestore-backed)."""

from __future__ import annotations

import random

from firebase_admin import firestore

import config as cfg
from services.game_manager import create_game


def _get_db():
    return firestore.client()


def run_matchmaking() -> list[str]:
    """Run one matchmaking pass. Returns list of created game IDs."""
    game_ids = []
    for mode, seats in cfg.MODES.items():
        game_ids.extend(_match_mode(mode, seats))
    return game_ids


def _match_mode(mode: str, players_needed: int) -> list[str]:
    """Match players for a given mode. Returns created game IDs.

    A 3p or 4p table seats the bots that have waited longest, in the order
    they joined, so the bots playing a mode share its games evenly. A 1v1
    game pairs the bot that has waited longest with the waiting bot closest
    to it in rating (with some randomness), not the bot it just played if
    another is waiting. Grouping the queue sorted by rating instead left the
    top-rated bots out whenever it did not divide into full tables, round
    after round: with six 4p bots playing, the two highest rated sat out
    most games. (Seating each 3p/4p table around the longest-waiting bot by
    rating still left the bots at either end of the ratings half the games
    of those in the middle.)"""
    db = _get_db()
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
    for qdoc in queue_docs:
        bid = qdoc.to_dict()["botId"]
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

    # 1v1: ratings, with jitter so the same bots are not always paired, and
    # each bot's last opponent
    rating = {}
    last_opp = {}
    if mode == "1v1":
        for bid in waiting:
            bot_doc = db.collection("bots").document(bid).get()
            bdata = bot_doc.to_dict() if bot_doc.exists else {}
            rating[bid] = (bdata.get(elo_key) or 1500.0) + random.gauss(0, 50)
            last_opp[bid] = bdata.get("lastOpponentId")

    game_ids = []
    while len(waiting) >= players_needed:
        first, rest = waiting[0], waiting[1:]
        if mode == "1v1":
            # The nearest in rating, but not the bot it just played (and that
            # just played it) if another is waiting
            rest.sort(key=lambda b: (last_opp.get(first) == b and last_opp.get(b) == first,
                                     abs(rating[b] - rating[first])))
        group = [first] + rest[:players_needed - 1]

        game_ids.append(create_game(mode, group))
        for bid in group:
            db.collection("queue").document(queue_id[bid]).delete()
        waiting = [b for b in waiting if b not in group]

    return game_ids
