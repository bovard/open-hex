"""The matchmaker seeds each game with the most uncertain, longest-waiting bot
and fills it with the bots nearest the seed in rating.

It once sorted the queue by rating and seated full tables from the bottom up,
so whoever did not fit, the highest rated, sat out round after round. Then it
seated bots in queue order (provisional bots first), which shared the games
evenly but put bots of any rating at one table: a bot rated far below the
rest spoils a strong game. Now the seed is the waiting bot with the highest
rating standard error times time queued, and the rest of its table are the
waiting bots nearest it in rating.
The Firebase and Google Cloud packages are stood in for, so no project or
credentials are needed. Run from the repository root:
    python3 -m unittest discover -s firebase/functions/tests -t firebase/functions
"""

from __future__ import annotations

import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.test_bots_api import _FakeDb, _stub_modules


def _import_matchmaker():
    names = list(_stub_modules()) + ["services.matchmaker", "services.game_manager",
                                     "services.hidden_info"]
    saved = {n: sys.modules.get(n) for n in names}
    try:
        sys.modules.update(_stub_modules())
        sys.modules.pop("services.matchmaker", None)
        sys.modules.pop("services.game_manager", None)
        return importlib.import_module("services.matchmaker")
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


# The live 4p ratings when the queue-order matchmaker was replaced
LIVE_4P = {"nn-b": 1684.5, "nn": 1660.1, "l12": 1574.6, "ppo": 1454.4, "l13": 1381.3, "catanrl": 1218.9}


class MatchmakerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mm = _import_matchmaker()

    def setUp(self):
        self.db = _FakeDb()
        self.now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        self.tables = []
        for name, target in (("_get_db", mock.Mock(return_value=self.db)),
                             ("create_game", self.create_game)):
            patcher = mock.patch.object(self.mm, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_game(self, mode, bot_ids):
        self.tables.append(list(bot_ids))
        return f"game-{len(self.tables)}"

    def bots(self, mode, ratings, se=None, **extra):
        """Bots with these ratings and a standard error of 50 unless `se`
        gives a bot's (None: no ratings refit has rated it)."""
        for bot_id, rating in ratings.items():
            doc = {"name": bot_id, f"elo{mode}": rating, **extra.get(bot_id, {})}
            bot_se = (se or {}).get(bot_id, 50.0)
            if bot_se is not None:
                doc[f"eloSe{mode}"] = bot_se
            self.db.collection("bots").document(bot_id).set(doc)

    def join(self, mode, *bot_ids):
        """Queue the bots now, in this order (the fake returns documents as inserted)."""
        for bot_id in bot_ids:
            self.db.collection("queue").document(f"{bot_id}_{mode}").set(
                {"botId": bot_id, "mode": mode, "joinedAt": self.now.isoformat()})

    def wait(self, seconds):
        self.now += timedelta(seconds=seconds)

    def queued(self):
        return [d["botId"] for d in self.db.data.get("queue", {}).values()]

    def match(self, mode, jitter=True):
        players = {"1v1": 2, "3p": 3, "4p": 4}[mode]
        if jitter:
            return self.mm._match_mode(mode, players, now=self.now)
        with mock.patch.object(self.mm.random, "gauss", return_value=0.0):
            return self.mm._match_mode(mode, players, now=self.now)


class TheSeed(MatchmakerCase):
    RATINGS = {"low": 1200, "high": 1800, "h2": 1750, "h3": 1700, "l2": 1250}

    def test_the_longest_waiting_seeds_when_equally_uncertain(self):
        self.bots("4p", {"a": 1500, "b": 1520, "c": 1480, "d": 1450, "e": 1800})
        self.join("4p", "e")
        self.wait(1)
        self.join("4p", "a", "b", "c", "d")
        self.match("4p", jitter=False)
        # e seeds, with the three nearest it in rating
        self.assertEqual(self.tables, [["e", "c", "a", "b"]])
        self.assertEqual(self.queued(), ["d"])

    def test_a_more_uncertain_bot_seeds_before_a_longer_waiting_one(self):
        # low: 50 x (240 + 60) = 15000; high: 300 x 60 = 18000
        self.bots("4p", self.RATINGS, se={"high": 300.0})
        self.join("4p", "low")
        self.wait(240)
        self.join("4p", "high", "h2", "h3", "l2")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["high", "l2", "h3", "h2"]])
        self.assertEqual(self.queued(), ["low"])

    def test_waiting_long_enough_outweighs_uncertainty(self):
        # low: 50 x (480 + 60) = 27000; high: 300 x 60 = 18000
        self.bots("4p", self.RATINGS, se={"high": 300.0})
        self.join("4p", "low")
        self.wait(480)
        self.join("4p", "high", "h2", "h3", "l2")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["low", "l2", "h3", "h2"]])
        self.assertEqual(self.queued(), ["high"])

    def test_a_bot_not_yet_rated_counts_as_most_uncertain(self):
        # low: 50 x (180 + 60) = 12000; high: PRIOR_SE (about 246) x 60
        self.bots("4p", self.RATINGS, se={"high": None})
        self.join("4p", "low")
        self.wait(180)
        self.join("4p", "high", "h2", "h3", "l2")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables[0][0], "high")


class ClosestTable(MatchmakerCase):
    def test_a_low_rated_bot_is_kept_out_of_a_strong_table(self):
        self.bots("4p", LIVE_4P)
        self.join("4p", "nn-b")
        self.wait(30)
        self.join("4p", "nn", "l12", "ppo", "l13", "catanrl")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["nn-b", "ppo", "l12", "nn"]])
        self.assertEqual(self.queued(), ["l13", "catanrl"])

    def test_a_low_rated_seed_gets_a_table_of_its_own_level(self):
        self.bots("4p", LIVE_4P)
        self.join("4p", "catanrl")
        self.wait(30)
        self.join("4p", "nn-b", "nn", "l12", "ppo", "l13")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["catanrl", "l13", "ppo", "l12"]])
        self.assertEqual(self.queued(), ["nn-b", "nn"])

    def test_tables_until_too_few_wait(self):
        self.bots("4p", {f"b{i}": 1500 + 10 * i for i in range(9)})
        self.join("4p", *(f"b{i}" for i in range(9)))
        self.assertEqual(len(self.match("4p")), 2)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(sorted(sum(self.tables, []) + self.queued()), sorted(f"b{i}" for i in range(9)))

    def test_too_few_queued(self):
        self.bots("3p", {"a": 1500, "b": 1500})
        self.join("3p", "a", "b")
        self.assertEqual(self.match("3p"), [])
        self.assertEqual(self.queued(), ["a", "b"])


class Rotation(MatchmakerCase):
    def rotate(self, ratings, rounds, se=None):
        """Each round a table is seated, plays for ten minutes and queues again;
        returns the tables and each bot's longest run of rounds sat out."""
        self.bots("4p", ratings, se=se)
        self.join("4p", *ratings)
        tables, sat_out, longest = [], dict.fromkeys(ratings, 0), dict.fromkeys(ratings, 0)
        for _ in range(rounds):
            self.tables.clear()
            self.assertEqual(len(self.match("4p")), 1)
            (table,) = self.tables
            tables.append(table)
            for bot in ratings:
                sat_out[bot] = 0 if bot in table else sat_out[bot] + 1
                longest[bot] = max(longest[bot], sat_out[bot])
            self.wait(600)
            self.join("4p", *table)
            self.wait(30)
        return tables, longest

    def test_six_bots_in_rotation(self):
        # Every table of four adjacent in rating holds the middle two of six,
        # so they play every round and the rest every other round (seated by
        # queue order, every bot played two rounds in three). Nobody sits out
        # more than two rounds running, the top and bottom bots never meet,
        # and the tables are closer.
        rounds = 300
        tables, longest = self.rotate(LIVE_4P, rounds)
        played = {b: sum(b in t for t in tables) for b in LIVE_4P}
        for bot, n in played.items():
            self.assertGreaterEqual(n, rounds * 0.49, (bot, played))
        self.assertEqual(played["l12"], rounds)
        self.assertEqual(played["ppo"], rounds)
        self.assertLessEqual(max(longest.values()), 2, longest)
        self.assertFalse([t for t in tables if "nn-b" in t and "catanrl" in t])
        spread = sum(max(LIVE_4P[b] for b in t) - min(LIVE_4P[b] for b in t) for t in tables)
        self.assertLess(spread / rounds, 300)   # seated by queue order: 350 on average

    def test_a_new_bot_seeds_ahead_of_bots_that_waited_a_little_longer(self):
        # With no games, its uncertainty (PRIOR_SE, about five times theirs)
        # outweighs up to about four minutes' more waiting: 246 x 60 against
        # 50 x 180
        ratings = dict(LIVE_4P, new=1350.0)
        self.bots("4p", ratings, se={"new": None})
        self.join("4p", *LIVE_4P)
        self.wait(120)
        self.join("4p", "new")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["new", "l13", "ppo", "l12"]])


class StaleEntries(MatchmakerCase):
    def game(self, bot_id, left=False):
        self.db.collection("games").document(f"g-{bot_id}").set(
            {"status": "active", "playerBotIds": [bot_id], "leftBotIds": [bot_id] if left else []})

    def test_a_bot_in_an_active_game_is_dropped_from_the_queue(self):
        self.bots("4p", {b: 1500 for b in "abcde"})
        self.join("4p", "a", "b", "c", "d", "e")
        self.game("a")
        self.match("4p")
        self.assertEqual(sorted(self.tables[0]), ["b", "c", "d", "e"])
        self.assertEqual(self.queued(), [])

    def test_a_game_it_left_does_not_hold_it_back(self):
        self.bots("4p", {b: 1500 for b in "abcd"})
        self.join("4p", "a", "b", "c", "d")
        self.game("a", left=True)
        self.match("4p")
        self.assertEqual(sorted(self.tables[0]), ["a", "b", "c", "d"])


class OneVersusOne(MatchmakerCase):
    def test_pairs_the_seed_with_the_nearest_rating(self):
        self.bots("1v1", {"a": 1200, "b": 1700, "c": 1250, "f": 1210})
        self.join("1v1", "a")
        self.wait(10)
        self.join("1v1", "b", "c", "f")
        self.assertEqual(self.match("1v1", jitter=False), ["game-1", "game-2"])
        self.assertEqual(self.tables, [["a", "f"], ["b", "c"]])
        self.assertEqual(self.queued(), [])

    def test_a_bot_without_a_rating_counts_as_1500(self):
        self.bots("1v1", {"a": None, "b": 1050, "c": 1900})
        self.join("1v1", "a", "b", "c")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["a", "c"]])

    def test_1v1_skips_the_bot_it_just_played(self):
        self.bots("1v1", {"a": 1500, "b": 1505, "c": 1700},
                  a={"lastOpponentId": "b"}, b={"lastOpponentId": "a"})
        self.join("1v1", "a", "b", "c")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["a", "c"]])
        self.assertEqual(self.queued(), ["b"])

    def test_1v1_rematch_when_nobody_else_waits(self):
        self.bots("1v1", {"a": 1500, "b": 1505},
                  a={"lastOpponentId": "b"}, b={"lastOpponentId": "a"})
        self.join("1v1", "a", "b")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["a", "b"]])

    def test_1v1_odd_queue_leaves_out_the_furthest_from_the_seed(self):
        self.bots("1v1", {"top": 1900, "x": 1500, "y": 1450})
        self.join("1v1", "top", "x", "y")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["top", "x"]])
        self.assertEqual(self.queued(), ["y"])


if __name__ == "__main__":
    unittest.main()
