"""The matchmaker picks a seed for each game, then seats the closest-rated
table around it.

The seed is the waiting bot with the highest seed priority: points for an
uncertain rating, for time since its last game, for time since it last
seeded, and for a rating near the top of the leaderboard; a bot idle for 30
minutes seeds first. The rest of the table are the waiting bots that make
the table of the smallest rating spread with the seed, whatever their own
priority. (Before, the seed was the bot with the highest standard error
times time queued: the top bot seeded 1% of tables and the two widest-rated
bots about 90%. Before that, bots were seated in queue order, which shared
games evenly but put bots of any rating at one table.)
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
            doc = {"name": bot_id, "mode": mode, f"elo{mode}": rating, **extra.get(bot_id, {})}
            bot_se = (se or {}).get(bot_id, 50.0)
            if bot_se is not None:
                doc[f"eloSe{mode}"] = bot_se
            self.db.collection("bots").document(bot_id).set(doc)

    def bot(self, bot_id):
        return self.db.data["bots"][bot_id]

    def ago(self, minutes):
        return (self.now - timedelta(minutes=minutes)).isoformat()

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


class SeedPriority(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.priority = staticmethod(_import_matchmaker().seed_priority)

    def test_points(self):
        # A settled bot at the top, just finished a game, never seeded: 6 + 10
        self.assertEqual(self.priority(50.0, 0.0, None, 0.0), (0, 16.0))
        # Idle 20 minutes: 2 points; seeded 60 minutes ago: 2 points
        self.assertEqual(self.priority(50.0, 20.0, 60.0, 0.0), (0, 14.0))
        # 400 below the top: half the top points
        self.assertEqual(self.priority(50.0, 0.0, 0.0, 400.0), (0, 5.0))
        # A standard error of 100: 8 points; a narrower one counts nothing
        self.assertEqual(self.priority(100.0, 0.0, 0.0, 0.0), (0, 18.0))
        self.assertEqual(self.priority(20.0, 0.0, 0.0, 0.0), (0, 10.0))

    def test_a_new_bot_outscores_any_settled_bot(self):
        new = self.priority(246.0, 0.0, None, 400.0)
        settled = self.priority(50.0, 29.0, 600.0, 0.0)
        self.assertGreater(new, settled)

    def test_time_since_seeding_counts_up_to_three_hours(self):
        self.assertEqual(self.priority(50.0, 0.0, 600.0, 0.0), self.priority(50.0, 0.0, None, 0.0))
        self.assertEqual(self.priority(50.0, 0.0, 180.0, 0.0), self.priority(50.0, 0.0, None, 0.0))

    def test_thirty_minutes_idle_seeds_first(self):
        guarded = self.priority(50.0, 30.0, 0.0, 1000.0)
        self.assertGreater(guarded, self.priority(246.0, 29.0, None, 0.0))
        self.assertGreater(self.priority(50.0, 45.0, 0.0, 1000.0), guarded)


class TheSeed(MatchmakerCase):
    def test_the_top_rated_seeds_when_all_else_is_equal(self):
        self.bots("4p", LIVE_4P)
        self.join("4p", *LIVE_4P)
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["nn-b", "ppo", "l12", "nn"]])
        self.assertEqual(self.queued(), ["l13", "catanrl"])

    def test_a_new_bot_seeds_first(self):
        self.bots("4p", dict(LIVE_4P, new=1350.0), se={"new": None})
        self.join("4p", *LIVE_4P, "new")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["new", "l13", "ppo", "l12"]])

    def test_a_bot_that_has_not_seeded_for_a_while_gets_its_own_table(self):
        # Every bot but the lowest rated has just seeded a game
        seeded = {b: {"lastSeededAt": self.ago(0)} for b in LIVE_4P if b != "catanrl"}
        self.bots("4p", LIVE_4P, **seeded)
        self.join("4p", *LIVE_4P)
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["catanrl", "l13", "ppo", "l12"]])

    def test_idle_time_counts_from_its_last_game(self):
        self.bots("1v1", {"a": 1500, "b": 1500}, b={"lastGameAt": self.ago(20)})
        self.join("1v1", "a", "b")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["b", "a"]])

    def test_or_from_joining_if_its_last_game_ended_later(self):
        # a left a game and queued again ten minutes ago; the game has just
        # ended. b finished a game five minutes ago.
        self.bots("1v1", {"a": 1500, "b": 1500})
        self.join("1v1", "a")
        self.wait(600)
        self.bot("a")["lastGameAt"] = self.ago(0)
        self.bot("b")["lastGameAt"] = self.ago(5)
        self.join("1v1", "b")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["a", "b"]])

    def test_a_bot_idle_thirty_minutes_seeds_first(self):
        self.bots("4p", dict(LIVE_4P, new=1350.0), se={"new": None},
                  catanrl={"lastGameAt": self.ago(31)})
        self.join("4p", *LIVE_4P, "new")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables[0][0], "catanrl")

    def test_the_top_is_the_leaderboards_rank_1_waiting_or_not(self):
        # a: seeded just now, 10 points at the top of those waiting, 5 below a
        # leader rated 2000. b: seeded 30 minutes ago (1 point), 100 below a.
        ratings = {"a": 1600, "b": 1500}
        extra = {"a": {"lastSeededAt": self.ago(0)}, "b": {"lastSeededAt": self.ago(30)}}
        self.bots("1v1", ratings, **extra)
        self.join("1v1", "a", "b")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["a", "b"]])

        self.setUp()
        self.bots("1v1", dict(ratings, leader=2000), leader={"rank1v1": 1}, **extra)
        self.join("1v1", "a", "b")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["b", "a"]])

    def test_the_seed_is_recorded(self):
        self.bots("4p", LIVE_4P)
        self.join("4p", *LIVE_4P)
        self.match("4p", jitter=False)
        self.assertEqual(self.bot("nn-b")["lastSeededAt"], self.now.isoformat())
        self.assertEqual([b for b in LIVE_4P if "lastSeededAt" in self.bot(b)], ["nn-b"])


class ClosestTable(MatchmakerCase):
    def test_a_low_rated_bot_is_kept_out_of_a_strong_table(self):
        self.bots("4p", dict(LIVE_4P, low=900.0))
        self.join("4p", "low", *LIVE_4P)
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["nn-b", "ppo", "l12", "nn"]])

    def test_the_table_ignores_the_others_priority(self):
        # l13 has waited 29 minutes, but nn-b seeds and l13 is not among the
        # closest to it
        self.bots("4p", LIVE_4P, l13={"lastGameAt": self.ago(29)})
        self.join("4p", *LIVE_4P)
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["nn-b", "ppo", "l12", "nn"]])

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
        """Each round a table is seated, plays for ten minutes and queues again
        thirty seconds after it ends; returns the tables and each bot's longest
        run of rounds sat out."""
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
            for bot in table:
                self.bot(bot)["lastGameAt"] = self.now.isoformat()
            self.wait(30)
            self.join("4p", *table)
        return tables, longest

    def test_six_bots_in_rotation(self):
        rounds = 300
        tables, longest = self.rotate(LIVE_4P, rounds)
        played = {b: sum(b in t for t in tables) for b in LIVE_4P}
        seeded = {b: sum(t[0] == b for t in tables) for b in LIVE_4P}
        # Every bot seeds. Nobody sits out more than four rounds: three to
        # pass the 30-minute guard, and one more when another bot past it
        # seeds first and its table leaves this one out. The top and bottom
        # bots never meet, the top bot plays more than the bottom one, and the
        # tables are closer than in queue order (350 on average).
        self.assertTrue(all(seeded.values()), seeded)
        self.assertLessEqual(max(longest.values()), 4, longest)
        self.assertFalse([t for t in tables if "nn-b" in t and "catanrl" in t])
        self.assertGreater(played["nn-b"], played["catanrl"])
        spread = sum(max(LIVE_4P[b] for b in t) - min(LIVE_4P[b] for b in t) for t in tables)
        self.assertLess(spread / rounds, 300)


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
        # b seeds (the top), then f (just above a)
        self.bots("1v1", {"a": 1200, "b": 1700, "c": 1250, "f": 1210})
        self.join("1v1", "a", "b", "c", "f")
        self.assertEqual(self.match("1v1", jitter=False), ["game-1", "game-2"])
        self.assertEqual(self.tables, [["b", "c"], ["f", "a"]])
        self.assertEqual(self.queued(), [])

    def test_a_bot_without_a_rating_counts_as_1500(self):
        self.bots("1v1", {"a": None, "b": 1050, "c": 1900})
        self.join("1v1", "a", "b", "c")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["c", "a"]])

    def test_1v1_skips_the_bot_it_just_played(self):
        self.bots("1v1", {"a": 1500, "b": 1505, "c": 1700},
                  a={"lastOpponentId": "b"},
                  b={"lastOpponentId": "a", "lastSeededAt": self.ago(0)},
                  c={"lastSeededAt": self.ago(0)})
        self.join("1v1", "a", "b", "c")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["a", "c"]])
        self.assertEqual(self.queued(), ["b"])

    def test_1v1_rematch_when_nobody_else_waits(self):
        self.bots("1v1", {"a": 1500, "b": 1505},
                  a={"lastOpponentId": "b"}, b={"lastOpponentId": "a"})
        self.join("1v1", "a", "b")
        self.match("1v1", jitter=False)
        self.assertEqual(sorted(self.tables[0]), ["a", "b"])

    def test_1v1_odd_queue_leaves_out_the_furthest_from_the_seed(self):
        self.bots("1v1", {"top": 1900, "x": 1500, "y": 1450})
        self.join("1v1", "top", "x", "y")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["top", "x"]])
        self.assertEqual(self.queued(), ["y"])


if __name__ == "__main__":
    unittest.main()
