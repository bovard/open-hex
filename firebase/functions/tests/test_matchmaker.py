"""The matchmaker seats provisional bots first, then the bots that waited longest.

It used to sort the queue by rating and seat full tables from the bottom up,
so whoever did not fit sat out: the highest rated. With six 4p bots playing,
the two highest rated (2s-NN-Small-Greedy-B and 2s-NN-Small-Greedy) sat out
most rounds; B had 9 games when its twin, registered the same minute, had 15.
Bots are now seated in order: provisional bots (fewer than 20 rated games)
first, fewest rated games first, so a new bot plays every game it queues for
until its rating settles; then the rest, longest waiting first. A 3p or 4p
table seats the first bots in that order; a 1v1 game pairs the first with the
waiting bot nearest its rating.
The Firebase and Google Cloud packages are stood in for, so no project or
credentials are needed. Run from the repository root:
    python3 -m unittest discover -s firebase/functions/tests -t firebase/functions
"""

from __future__ import annotations

import importlib
import sys
import unittest
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


# The live 4p ratings when the problem was found
LIVE_4P = {"nn-b": 1684.5, "nn": 1660.1, "l12": 1574.6, "ppo": 1454.4, "l13": 1381.3, "catanrl": 1218.9}


class MatchmakerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mm = _import_matchmaker()

    def setUp(self):
        self.db = _FakeDb()
        self.clock = 0
        self.tables = []
        for name, target in (("_get_db", mock.Mock(return_value=self.db)),
                             ("create_game", self.create_game)):
            patcher = mock.patch.object(self.mm, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_game(self, mode, bot_ids):
        self.tables.append(list(bot_ids))
        return f"game-{len(self.tables)}"

    def bots(self, mode, ratings, rated=None, **extra):
        """Bots with these ratings; established (40 rated games) unless `rated`
        gives a bot's rated games (None: no ratings refit has counted any)."""
        for bot_id, rating in ratings.items():
            doc = {"name": bot_id, f"elo{mode}": rating, **extra.get(bot_id, {})}
            games = (rated or {}).get(bot_id, 40)
            if games is not None:
                doc[f"ratedGames{mode}"] = games
            self.db.collection("bots").document(bot_id).set(doc)

    def join(self, mode, *bot_ids):
        """Queue the bots, in this order (the fake returns documents as inserted)."""
        for bot_id in bot_ids:
            self.clock += 1
            self.db.collection("queue").document(f"{bot_id}_{mode}").set(
                {"botId": bot_id, "mode": mode, "joinedAt": f"t{self.clock:05d}"})

    def queued(self):
        return [d["botId"] for d in self.db.data.get("queue", {}).values()]

    def match(self, mode, jitter=True):
        players = {"1v1": 2, "3p": 3, "4p": 4}[mode]
        if jitter:
            return self.mm._match_mode(mode, players)
        with mock.patch.object(self.mm.random, "gauss", return_value=0.0):
            return self.mm._match_mode(mode, players)


class LongestWaitingFirst(MatchmakerCase):
    def test_a_table_seats_the_longest_waiting_in_join_order(self):
        self.bots("4p", {"a": 1200, "b": 1700, "c": 1250, "d": 1300, "e": 1650, "f": 1210})
        self.join("4p", "a", "b", "c", "d", "e", "f")
        self.assertEqual(self.match("4p"), ["game-1"])
        self.assertEqual(self.tables, [["a", "b", "c", "d"]])
        self.assertEqual(self.queued(), ["e", "f"])

    def test_the_top_rated_waiting_longest_are_seated(self):
        # The case that starved: the two highest rated waited a round, and four
        # lower-rated bots joined after them
        self.bots("4p", LIVE_4P)
        self.join("4p", "nn-b", "nn", "l12", "ppo", "l13", "catanrl")
        self.match("4p", jitter=False)
        self.assertEqual(self.tables, [["nn-b", "nn", "l12", "ppo"]])

    def test_six_bots_in_rotation_share_the_games(self):
        # Each round the bots seated play and queue again, behind those left
        # waiting. Nobody waits two rounds running, and the games are shared
        # evenly (4 of 6 seats: two thirds of the rounds each; the old
        # matchmaker gave the top two about 5% and 10%).
        self.bots("4p", LIVE_4P)
        self.join("4p", *LIVE_4P)
        rounds = 300
        played = dict.fromkeys(LIVE_4P, 0)
        waited = dict.fromkeys(LIVE_4P, 0)
        longest_wait = 0
        for _ in range(rounds):
            self.tables.clear()
            self.assertEqual(len(self.match("4p")), 1)
            (table,) = self.tables
            for bot in LIVE_4P:
                if bot in table:
                    played[bot] += 1
                    waited[bot] = 0
                else:
                    waited[bot] += 1
                    longest_wait = max(longest_wait, waited[bot])
            self.join("4p", *table)
        self.assertEqual(longest_wait, 1)
        for bot, n in played.items():
            self.assertGreater(n, rounds * 0.6, (bot, played))
            self.assertLess(n, rounds * 0.73, (bot, played))

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



class ProvisionalFirst(MatchmakerCase):
    def test_provisional_bots_first_fewest_rated_games_first(self):
        self.bots("4p", {b: 1500 for b in "abcdef"},
                  rated={"a": 25, "b": 30, "c": 5, "d": 22, "e": None, "f": 19})
        self.join("4p", "a", "b", "c", "d", "e", "f")
        self.match("4p")
        # e (no rated games yet), c (5) and f (19), then a, the longest waiting
        self.assertEqual(self.tables, [["e", "c", "f", "a"]])
        self.assertEqual(self.queued(), ["b", "d"])

    def test_a_new_bot_plays_every_game_until_it_is_rated(self):
        # Six established bots in rotation; a new one joins. It plays every
        # round until it has 20 rated games (the established bots share the
        # other three seats, none waiting two rounds running), then takes its
        # turn with the rest.
        bots = dict(LIVE_4P, new=1500)
        self.bots("4p", bots, rated={"new": None})
        self.join("4p", *LIVE_4P, "new")
        played = dict.fromkeys(bots, 0)
        waited = dict.fromkeys(LIVE_4P, 0)
        first_rounds = []
        for rnd in range(160):
            self.tables.clear()
            self.match("4p")
            (table,) = self.tables
            for bot in table:
                played[bot] += 1
                doc = self.db.data["bots"][bot]   # as the ratings refit counts it
                doc["ratedGames4p"] = doc.get("ratedGames4p", 0) + 1
            if rnd < 20:
                first_rounds.append("new" in table)
                for bot in LIVE_4P:
                    waited[bot] = 0 if bot in table else waited[bot] + 1
                    self.assertLessEqual(waited[bot], 1, (rnd, bot))
            if rnd == 19:
                played = dict.fromkeys(bots, 0)
            self.join("4p", *table)
        self.assertEqual(first_rounds, [True] * 20)
        # Rated from then on: 4 seats of 7, about 57% of the rounds each
        for bot, n in played.items():
            self.assertGreater(n, 140 * 0.5, (bot, played))
            self.assertLess(n, 140 * 0.65, (bot, played))

    def test_1v1_pairs_a_new_bot_first(self):
        self.bots("1v1", {"old": 1500, "mid": 1600, "new": 1500}, rated={"new": 0})
        self.join("1v1", "old", "mid", "new")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["new", "old"]])
        self.assertEqual(self.queued(), ["mid"])


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
    def test_pairs_the_longest_waiting_bot_with_the_nearest_rating(self):
        self.bots("1v1", {"a": 1200, "b": 1700, "c": 1250, "f": 1210})
        self.join("1v1", "a", "b", "c", "f")
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

    def test_1v1_odd_queue_leaves_out_the_newest_not_the_top_rated(self):
        self.bots("1v1", {"top": 1900, "x": 1500, "y": 1450})
        self.join("1v1", "top", "x", "y")
        self.match("1v1", jitter=False)
        self.assertEqual(self.tables, [["top", "x"]])
        self.assertEqual(self.queued(), ["y"])


if __name__ == "__main__":
    unittest.main()
