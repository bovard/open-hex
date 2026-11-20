"""Who places first: `game_manager.create_game` seats the matched bots.

The matchmaker hands each group over sorted by rating, so seating the bots in
the order they arrive gave seat 0 (the first to place, and the first to move)
to the lower-rated bot almost every time. The seats are now drawn from the
game's seed. The Firebase and Google Cloud packages are stood in for, so no
project or credentials are needed. Run from the repository root:
    python3 -m unittest discover -s firebase/functions/tests -t firebase/functions
"""

from __future__ import annotations

import importlib
import json
import sys
import unittest
from unittest import mock

from tests.test_bots_api import _stub_modules


def _import_game_manager():
    names = list(_stub_modules()) + ["services.game_manager", "services.hidden_info"]
    saved = {n: sys.modules.get(n) for n in names}
    try:
        sys.modules.update(_stub_modules())
        sys.modules.pop("services.game_manager", None)
        return importlib.import_module("services.game_manager")
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


class SeatingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gm = _import_game_manager()

    def create(self, mode, bot_ids, seed):
        """The game document create_game writes, for these bots in matchmaker order."""
        db = mock.MagicMock()
        db.collection.return_value.document.return_value.get.return_value.to_dict.return_value = {"name": "bot"}
        with mock.patch.object(self.gm, "_get_db", return_value=db):
            self.gm.create_game(mode, list(bot_ids), seed=seed)
        return db.batch.return_value.set.call_args_list[0][0][1]

    def test_first_seat_is_drawn_not_taken_from_matchmaker_order(self):
        games = 400
        firsts = sum(self.create("1v1", ["lower", "higher"], seed)["players"][0]["botId"] == "lower"
                     for seed in range(games))
        # a fair draw: 200 expected, 10 standard deviations each side is 100..300
        self.assertGreater(firsts, 100)
        self.assertLess(firsts, 300)

    def test_seat_order_is_turn_order(self):
        for mode, bots in (("1v1", ["a", "b"]), ("3p", ["a", "b", "c"]), ("4p", ["a", "b", "c", "d"])):
            for seed in range(20):
                doc = self.create(mode, bots, seed)
                colors = [p["color"] for p in sorted(doc["players"], key=lambda p: p["seat"])]
                self.assertEqual(colors, json.loads(doc["turnOrderJson"]))
                self.assertEqual(sorted(doc["playerBotIds"]), sorted(bots))
                self.assertEqual([p["botId"] for p in doc["players"]], doc["playerBotIds"])

    def test_same_seed_same_seats(self):
        a = self.create("1v1", ["lower", "higher"], 12345)
        b = self.create("1v1", ["lower", "higher"], 12345)
        self.assertEqual(a["players"], b["players"])


if __name__ == "__main__":
    unittest.main()
