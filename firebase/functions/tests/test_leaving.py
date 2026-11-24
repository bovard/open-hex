"""A seat that strikes out of a 3p or 4p game leaves it, and play goes on.

Three missed deadlines in a row used to abort the game and hand the win to
the best-placed seat not at fault, often a game far from over. While two
other seats remain, the seat now leaves (`leaveGame`, made by the server
only) and the game goes on as if it had never been there: its cards go back
to the bank, its buildings stay on the board but produce nothing, it is no
one's steal target or trading partner, an award it held goes to whoever else
qualifies, and its turns are skipped. Run from the repository root:
    python3 -m unittest discover -s firebase/functions/tests -t firebase/functions
"""

from __future__ import annotations

import importlib
import json
import random
import sys
import unittest
from unittest import mock

from openhex import (
    Action, GameConfig, GameState, OpenHexEngine, PendingAction, PlayerColor,
    generate_standard_board, from_json, to_json,
)
from openhex.board import Road, VertexBuilding
from openhex.dice import BalancedDice
from openhex.json_codec import _action_from_json
from openhex.state import DiceResult
from openhex.types import DiceMode, Edge, Resource, TERRAIN_RESOURCE
from tests.test_bots_api import _FakeDb, _import_main, _stub_modules, flask
from tests.test_trading import main_phase

RED, BLUE, WHITE, ORANGE = PlayerColor.RED, PlayerColor.BLUE, PlayerColor.WHITE, PlayerColor.ORANGE
WOOD, BRICK, SHEEP, WHEAT, ORE = (Resource.WOOD, Resource.BRICK, Resource.SHEEP,
                                  Resource.WHEAT, Resource.ORE)


def cards_in_play(s: GameState) -> int:
    return sum(s.bank.values()) + sum(sum(p.hand.values()) for p in s.players)


class LeavingCase(unittest.TestCase):
    def setUp(self):
        self.engine = OpenHexEngine(rng=random.Random(1))

    def act(self, s, player, type_, **fields):
        result = self.engine.execute_action(s, Action(type=type_, player=player, **fields))
        self.assertTrue(result.success, f"{type_} by {player.value} failed: {result.error}")
        return result.state, [to_json(e) for e in result.events]

    def refused(self, s, player, type_, **fields) -> str:
        result = self.engine.execute_action(s, Action(type=type_, player=player, **fields))
        self.assertFalse(result.success, f"{type_} by {player.value} should be refused")
        return result.error

    def leave(self, s, color):
        return self.act(s, color, "leaveGame")

    def end_turn(self, s):
        """The current player ends its turn (dice taken as rolled, nothing pending)."""
        s.dice, s.pending_action = DiceResult(2, 3), None
        s, _ = self.act(s, s.current_player, "endTurn")
        return s

    def producing_tile(self, s):
        """A resource tile without the robber, and two of its vertices that are not neighbours."""
        tile = next(t for t in s.board.tiles
                    if t.terrain in TERRAIN_RESOURCE and t.location != s.board.robber)
        v = tile.location.vertices()
        return tile, v[0], v[2]


class CurrentPlayerLeaves(LeavingCase):
    def test_its_turn_ends_and_its_cards_go_to_the_bank(self):
        s = main_phase()
        s, _ = self.act(s, RED, "proposeTrade", offering={WOOD: 1}, requesting={ORE: 1})
        total = cards_in_play(s)
        bank_wood = s.bank[WOOD]
        s, events = self.leave(s, RED)
        self.assertEqual(events, [
            {"type": "tradeCancelled", "player": "red", "tradeId": 1, "reason": "currentPlayerMoved"},
            {"type": "playerLeft", "player": "red",
             "resources": {"wood": 3, "brick": 3, "sheep": 3, "wheat": 3, "ore": 3}},
            {"type": "turnEnded", "player": "red", "turnNumber": 1},
        ])
        self.assertEqual(s.get_player(RED).hand, {})
        self.assertEqual(s.bank[WOOD], bank_wood + 3)
        self.assertEqual(cards_in_play(s), total)
        self.assertEqual(s.left_players, [RED])
        self.assertEqual((s.current_player, s.pending_action.type, s.dice), (BLUE, "rollDice", None))
        self.assertEqual(s.trades, [])

    def test_mid_turn_with_the_robber_owed(self):
        s = main_phase()
        s.pending_action = PendingAction(type="moveRobber")
        s, _ = self.leave(s, RED)
        self.assertEqual((s.current_player, s.pending_action.type), (BLUE, "rollDice"))


class PlayGoesOnWithoutIt(LeavingCase):
    def test_turns_skip_the_seat_that_left(self):
        s = main_phase()
        s, _ = self.leave(s, WHITE)
        order = []
        for _ in range(6):
            s = self.end_turn(s)
            order.append(s.current_player)
        self.assertEqual(order, [BLUE, ORANGE, RED, BLUE, ORANGE, RED])
        self.assertEqual(s.seats_in_play(), [RED, BLUE, ORANGE])

    def test_its_buildings_produce_nothing(self):
        s = main_phase()
        tile, v1, v2 = self.producing_tile(s)
        s.board.settlements += [VertexBuilding(vertex=v1, player=WHITE),
                                VertexBuilding(vertex=v2, player=BLUE)]
        s, _ = self.leave(s, WHITE)
        res = TERRAIN_RESOURCE[tile.terrain]
        # One card left in the bank: shared with a second claimant, nobody would get it
        s.bank[res], s.get_player(BLUE).hand[res] = 1, 0
        events = []
        self.engine._distribute_resources(s, tile.number, events)
        production = events[0].production
        self.assertNotIn(WHITE, production)
        self.assertEqual(production[BLUE][res], 1)
        self.assertEqual(s.get_player(WHITE).hand, {})

    def test_nothing_to_steal_from_it(self):
        s = main_phase()
        tile, v1, _ = self.producing_tile(s)
        s.board.settlements.append(VertexBuilding(vertex=v1, player=WHITE))
        s, _ = self.leave(s, WHITE)
        s.get_player(WHITE).hand = {WOOD: 1}   # even holding a card
        self.assertEqual(self.engine._get_steal_targets(s, RED, tile.location), [])
        s.dice, s.pending_action = DiceResult(3, 4), PendingAction(type="moveRobber")
        s, events = self.act(s, RED, "moveRobber", location=tile.location)
        self.assertEqual([e["type"] for e in events], ["robberMoved"])
        self.assertIsNone(s.pending_action)

    def test_offers_go_to_the_seats_in_play(self):
        s = main_phase()
        s, _ = self.leave(s, ORANGE)
        s, _ = self.act(s, RED, "proposeTrade", offering={WOOD: 1}, requesting={ORE: 1})
        self.assertEqual(self.refused(s, ORANGE, "acceptTrade", trade_id=1), "You have left this game")
        s, _ = self.act(s, BLUE, "rejectTrade", trade_id=1)
        s, events = self.act(s, WHITE, "rejectTrade", trade_id=1)
        self.assertEqual(events[-1], {"type": "tradeCancelled", "player": "red", "tradeId": 1,
                                      "reason": "allRejected"})

    def test_a_seat_that_left_makes_no_move(self):
        s = main_phase()
        s, _ = self.leave(s, BLUE)
        s = self.end_turn(s)
        self.assertEqual(s.current_player, WHITE)
        for type_ in ("rollDice", "endTurn", "leaveGame"):
            self.assertEqual(self.refused(s, BLUE, type_), "You have left this game")

    def test_balanced_dice_share_the_sevens_among_the_seats_in_play(self):
        s, _ = self.leave(main_phase(), WHITE)
        s.config.dice_mode, s.balanced_dice = DiceMode.BALANCED, BalancedDice()
        with mock.patch.object(BalancedDice, "draw", return_value=(3, 4)) as draw:
            self.engine._roll(s)
        self.assertEqual(draw.call_args.kwargs["num_players"], 3)

    def test_its_sevens_and_streak_leave_with_it(self):
        s = main_phase()
        s.balanced_dice = BalancedDice(sevens_by_player={"white": 3, "blue": 1},
                                       seven_streak_player="white", seven_streak_count=2)
        s, _ = self.leave(s, WHITE)
        bd = s.balanced_dice
        self.assertEqual((bd.sevens_by_player, bd.seven_streak_player, bd.seven_streak_count),
                         ({"blue": 1}, None, 0))


class Discards(LeavingCase):
    def test_a_discard_it_owed_is_dropped(self):
        s = main_phase()
        s.dice = DiceResult(3, 4)
        s.pending_action = PendingAction(type="discardResources", players={WHITE: 7, BLUE: 7})
        s, _ = self.leave(s, WHITE)
        self.assertEqual(s.pending_action.players, {BLUE: 7})
        self.assertEqual(s.current_player, RED)

    def test_the_robber_follows_when_it_was_the_last_to_discard(self):
        s = main_phase()
        s.dice = DiceResult(3, 4)
        s.pending_action = PendingAction(type="discardResources", players={WHITE: 7})
        s, _ = self.leave(s, WHITE)
        self.assertEqual((s.current_player, s.pending_action.type), (RED, "moveRobber"))

    def test_its_seven_still_costs_the_others_their_discards(self):
        # RED rolled the 7 and leaves before discarding: BLUE still discards,
        # then RED's turn ends with the robber where it was
        s = main_phase()
        s.dice = DiceResult(3, 4)
        robber = s.board.robber
        s.pending_action = PendingAction(type="discardResources", players={RED: 7, BLUE: 7})
        s, events = self.leave(s, RED)
        self.assertEqual([e["type"] for e in events], ["playerLeft"])
        self.assertEqual(s.pending_action.players, {BLUE: 7})
        self.assertEqual(s.players_to_act(), [BLUE])
        s, events = self.act(s, BLUE, "discardResources",
                             resources={WOOD: 3, BRICK: 3, SHEEP: 1})
        self.assertEqual([e["type"] for e in events], ["resourcesDiscarded", "turnEnded"])
        self.assertEqual(events[-1], {"type": "turnEnded", "player": "red", "turnNumber": 1})
        self.assertEqual(sum(s.get_player(BLUE).hand.values()), 8)
        self.assertEqual((s.current_player, s.pending_action.type, s.board.robber),
                         (BLUE, "rollDice", robber))

    def test_its_turn_ends_when_it_was_the_last_to_discard(self):
        s = main_phase()
        s.dice = DiceResult(3, 4)
        s.pending_action = PendingAction(type="discardResources", players={RED: 7})
        s, events = self.leave(s, RED)
        self.assertEqual([e["type"] for e in events], ["playerLeft", "turnEnded"])
        self.assertEqual((s.current_player, s.pending_action.type), (BLUE, "rollDice"))


class Awards(LeavingCase):
    def rings(self, s, colors):
        """A road of five along the edge of a tile for each color, on tiles that share no vertex."""
        used = set()
        for color in colors:
            tile = next(t for t in s.board.tiles if not used & set(t.location.vertices()))
            v = tile.location.vertices()
            used |= set(v)
            s.board.roads += [Road(edge=Edge.between(v[i], v[i + 1]), player=color) for i in range(5)]

    def test_longest_road_goes_to_the_next_longest(self):
        s = main_phase()
        self.rings(s, [WHITE, BLUE])
        s.longest_road_player = WHITE        # built first, keeps it on the tie
        self.engine._update_longest_road(s, [])
        self.assertEqual(s.longest_road_player, WHITE)
        s, events = self.leave(s, WHITE)
        self.assertIn({"type": "longestRoadAwarded", "player": "blue", "length": 5}, events)
        self.assertEqual(s.longest_road_player, BLUE)

    def test_longest_road_tied_after_it_left_goes_to_nobody(self):
        s = main_phase()
        self.rings(s, [WHITE, BLUE, ORANGE])
        s.longest_road_player = WHITE
        s, events = self.leave(s, WHITE)
        self.assertIn({"type": "longestRoadAwarded", "length": 0}, events)
        self.assertIsNone(s.longest_road_player)

    def test_longest_road_tied_without_it_goes_to_the_other(self):
        s = main_phase()
        self.rings(s, [WHITE, BLUE])
        self.engine._update_longest_road(s, [])
        self.assertIsNone(s.longest_road_player)      # tied, nobody had it first
        s, events = self.leave(s, WHITE)
        self.assertIn({"type": "longestRoadAwarded", "player": "blue", "length": 5}, events)

    def test_largest_army_tied_without_it_goes_to_the_other(self):
        s = main_phase()
        for color, n in ((WHITE, 4), (BLUE, 3), (ORANGE, 3)):
            s.get_player(color).knights_played = n
        s.largest_army_player = WHITE
        s, _ = self.leave(s, WHITE)                   # nobody: blue and orange tie
        s, events = self.leave(s, ORANGE)
        self.assertIn({"type": "largestArmyAwarded", "player": "blue", "knights": 3}, events)
        self.assertEqual(s.largest_army_player, BLUE)

    def test_an_award_held_by_another_seat_stays_put(self):
        s = main_phase()
        self.rings(s, [BLUE])
        s.longest_road_player = BLUE
        s.get_player(BLUE).knights_played = 3
        s.largest_army_player = BLUE
        s, events = self.leave(s, WHITE)
        self.assertEqual([e["type"] for e in events], ["playerLeft"])
        self.assertEqual((s.longest_road_player, s.largest_army_player), (BLUE, BLUE))

    def test_largest_army_goes_to_the_most_knights(self):
        s = main_phase()
        for color, n in ((WHITE, 4), (BLUE, 3), (ORANGE, 2)):
            s.get_player(color).knights_played = n
        s.largest_army_player = WHITE
        s, events = self.leave(s, WHITE)
        self.assertIn({"type": "largestArmyAwarded", "player": "blue", "knights": 3}, events)
        self.assertEqual(s.largest_army_player, BLUE)

    def test_largest_army_tied_goes_to_nobody_until_a_knight_breaks_the_tie(self):
        s = main_phase()
        for color, n in ((WHITE, 4), (BLUE, 3), (ORANGE, 3)):
            s.get_player(color).knights_played = n
        s.largest_army_player = WHITE
        s, events = self.leave(s, WHITE)
        self.assertIn({"type": "largestArmyAwarded", "knights": 0}, events)
        self.assertIsNone(s.largest_army_player)
        events = []
        self.engine._update_largest_army(s, BLUE, events)      # still 3 against 3
        self.assertIsNone(s.largest_army_player)
        s.get_player(BLUE).knights_played = 4
        self.engine._update_largest_army(s, BLUE, events)
        self.assertEqual(s.largest_army_player, BLUE)

    def test_first_to_three_knights_still_takes_it(self):
        s = main_phase()
        s.get_player(BLUE).knights_played = 3
        s.get_player(ORANGE).knights_played = 2
        self.engine._update_largest_army(s, BLUE, [])
        self.assertEqual(s.largest_army_player, BLUE)


class Setup(LeavingCase):
    def new_game(self):
        colors = [RED, BLUE, WHITE, ORANGE]
        return GameState.new_game(GameConfig(num_players=4), generate_standard_board(7), colors)

    def place(self, s):
        """The current seat places its settlement and road (the first legal spots)."""
        color = s.current_player
        for _ in range(2):
            s, _ = self.act(s, color, **_fields(self.engine.default_action(s, color)))
        return s

    def test_a_seat_that_left_places_nothing_more(self):
        s = self.new_game()
        s, _ = self.leave(s, BLUE)
        order = []
        while s.is_setup_phase:
            order.append(s.current_player)
            s = self.place(s)
        self.assertEqual(order, [RED, WHITE, ORANGE, ORANGE, WHITE, RED])
        self.assertEqual((s.current_player, s.pending_action.type), (RED, "rollDice"))

    def test_first_seat_leaving_hands_the_first_turn_on(self):
        s = self.new_game()
        s = self.place(s)
        s, _ = self.act(s, BLUE, **_fields(self.engine.default_action(s, BLUE)))  # settlement only
        s, events = self.leave(s, BLUE)
        self.assertEqual([e["type"] for e in events], ["playerLeft"])
        self.assertEqual((s.current_player, s.pending_action.type), (WHITE, "placeInitialSettlement"))
        self.assertEqual(s.board.player_settlement_count(BLUE), 1)   # its settlement stays
        s, _ = self.leave(s, RED)
        while s.is_setup_phase:
            s = self.place(s)
        self.assertEqual(s.current_player, WHITE)


class Limits(LeavingCase):
    def test_two_seats_must_remain(self):
        s = main_phase(colors=(RED, BLUE, WHITE))
        s, _ = self.leave(s, WHITE)
        self.assertEqual(self.refused(s, BLUE, "leaveGame"),
                         "Only two seats are playing; the game ends instead")
        duel = main_phase(colors=(RED, BLUE))
        self.assertEqual(self.refused(duel, BLUE, "leaveGame"),
                         "Only two seats are playing; the game ends instead")

    def test_state_round_trip(self):
        s, _ = self.leave(main_phase(), WHITE)
        data = json.loads(json.dumps(to_json(s)))
        self.assertEqual(data["leftPlayers"], ["white"])
        self.assertEqual(from_json(data, GameState).left_players, [WHITE])
        del data["leftPlayers"]
        self.assertEqual(from_json(data, GameState).left_players, [])


def _fields(action: Action) -> dict:
    return {"type_": action.type, "vertex": action.vertex, "vertices": action.vertices}


# ---------------------------------------------------------------------------
# The server: check_timeouts takes a struck-out seat out
# ---------------------------------------------------------------------------

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


class StrikeOut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gm = _import_game_manager()

    def check_timeouts(self, state, strikes):
        """Run check_timeouts on one overdue game; process_action runs the engine."""
        players = [{"botId": f"bot-{c.value}", "name": c.value, "color": c.value, "seat": i}
                   for i, c in enumerate(state.turn_order)]
        store = {"state": json.dumps(to_json(state)),
                 "game": {"status": "active", "players": players, "timeoutStrikes": dict(strikes)}}
        self.made = []

        def process_action(game_id, bot_id, action_dict, *, duration_ms=None, auto=False):
            # As the real one: no move in a finished game, and a win completes it
            if store["game"]["status"] != "active":
                return {"success": False, "events": [], "error": "Game is not active"}
            color = bot_id.removeprefix("bot-")
            s = from_json(json.loads(store["state"]), GameState)
            r = OpenHexEngine(rng=random.Random(0)).execute_action(
                s, _action_from_json({**action_dict, "player": color}))
            self.made.append((color, action_dict["type"]))
            if r.success:
                store["state"] = json.dumps(to_json(r.state))
                if r.state.winner is not None:
                    store["game"].update(status="completed",
                                         winnerBotId=f"bot-{r.state.winner.value}")
                    self.records(game_id)
            return {"success": r.success, "events": [], "error": r.error}

        db = mock.MagicMock()
        snap = mock.MagicMock(id="g")
        snap.to_dict.side_effect = lambda: dict(store["game"])
        db.collection.return_value.where.return_value.where.return_value.stream.return_value = [snap]
        game_ref = db.collection.return_value.document.return_value
        game_ref.get.return_value.to_dict.side_effect = lambda: dict(store["game"])
        game_ref.update.side_effect = lambda d: store["game"].update(d)
        self.records = mock.MagicMock()
        with mock.patch.multiple(self.gm, _get_db=mock.Mock(return_value=db),
                                 FieldFilter=mock.MagicMock(),
                                 process_action=process_action,
                                 load_state_json=lambda gid, game, db=None: store["state"],
                                 _update_records=self.records,
                                 _update_head_to_head=mock.MagicMock(),
                                 _save_replay_to_gcs=mock.MagicMock()):
            aborted = self.gm.check_timeouts()
        return aborted, store["game"], from_json(json.loads(store["state"]), GameState)

    def test_4p_seat_leaves_and_the_game_goes_on(self):
        aborted, game, s = self.check_timeouts(main_phase(), {"bot-red": 2})
        self.assertEqual(aborted, 0)
        self.assertEqual(self.made, [("red", "leaveGame")])
        self.assertEqual(game["status"], "active")
        self.assertEqual(game["timeoutStrikes"], {"bot-red": 3})
        self.assertEqual((s.left_players, s.current_player), ([RED], BLUE))
        self.records.assert_not_called()

    def test_a_strike_short_of_three_still_moves_for_the_seat(self):
        aborted, game, s = self.check_timeouts(main_phase(), {"bot-red": 1})
        self.assertEqual((aborted, self.made), (0, [("red", "endTurn")]))
        self.assertEqual((s.left_players, s.current_player), ([], BLUE))

    def test_1v1_is_aborted_for_the_other_seat(self):
        aborted, game, s = self.check_timeouts(main_phase(colors=(RED, BLUE)), {"bot-red": 2})
        self.assertEqual((aborted, self.made), (1, []))
        self.assertEqual((game["status"], game["winnerBotId"]), ("aborted", "bot-blue"))

    def test_last_two_seats_abort_for_the_one_in_play_not_the_one_that_left(self):
        s = main_phase(colors=(RED, BLUE, WHITE))
        tile, v1, v2 = LeavingCase.producing_tile(None, s)
        s.board.settlements += [VertexBuilding(vertex=v1, player=WHITE),
                                VertexBuilding(vertex=v2, player=WHITE)]   # the leader
        s.left_players = [WHITE]
        aborted, game, _ = self.check_timeouts(s, {"bot-red": 2})
        self.assertEqual((aborted, self.made), (1, []))
        self.assertEqual(game["winnerBotId"], "bot-blue")

    def test_a_leave_that_wins_the_game_for_another_seat_is_not_aborted(self):
        # RED and BLUE strike out on their discards together. RED leaving
        # passes Largest Army to WHITE, on its turn and at 8 VP: WHITE wins,
        # and the game stays won, not aborted, with its result counted once.
        s = main_phase()
        s.current_player = WHITE
        s.dice = DiceResult(3, 4)
        s.pending_action = PendingAction(type="discardResources", players={RED: 7, BLUE: 7})
        s.get_player(RED).knights_played, s.largest_army_player = 4, RED
        s.get_player(WHITE).knights_played = 3
        spots = []
        for tile in s.board.tiles:
            for v in tile.location.vertices():
                if all(v != w and v not in w.adjacent_vertices() for w in spots):
                    spots.append(v)
        s.board.cities += [VertexBuilding(vertex=v, player=WHITE) for v in spots[:4]]
        self.assertEqual(s.compute_vp(WHITE), 8)
        aborted, game, s = self.check_timeouts(s, {"bot-red": 2, "bot-blue": 2})
        self.assertEqual(aborted, 0)
        self.assertEqual(self.made, [("red", "leaveGame")])
        self.assertEqual((game["status"], game["winnerBotId"]), ("completed", "bot-white"))
        self.assertEqual(s.winner, WHITE)
        self.assertEqual(self.records.call_count, 1)

    def test_bots_cannot_leave_a_game(self):
        with mock.patch.object(self.gm, "_get_db", side_effect=AssertionError("no db call")):
            r = self.gm.process_action("g", "bot-red", {"type": "leaveGame"})
        self.assertEqual(r, {"success": False, "events": [],
                             "error": "Unknown action type: leaveGame"})


@unittest.skipIf(flask is None, "Flask is not installed")
class LeftGamesHoldNoBotBack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main()

    def setUp(self):
        self.db = _FakeDb()
        patcher = mock.patch.object(self.main, "_get_db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.main.app.test_client()
        r = self.client.post("/api/bots", json={"name": "quad", "mode": "4p"})
        self.bot_id, key = r.get_json()["bot"]["id"], r.get_json()["api_key"]
        self.auth = {"Authorization": f"Bearer {key}"}

    def game(self, game_id, left):
        self.db.collection("games").document(game_id).set({
            "status": "active", "mode": "4p", "players": [], "playerBotIds": [self.bot_id],
            "leftBotIds": [self.bot_id] if left else [], "createdAt": game_id})

    def test_queue_join(self):
        self.game("a", left=True)
        r = self.client.post("/api/queue/join", json={"mode": "4p"}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.db.data["queue"].clear()
        self.game("b", left=False)
        r = self.client.post("/api/queue/join", json={"mode": "4p"}, headers=self.auth)
        self.assertEqual(r.status_code, 409)

    def test_active_games_leave_out_the_ones_it_left(self):
        self.game("a", left=True)
        self.game("b", left=False)
        r = self.client.get("/api/games?status=active", headers=self.auth)
        self.assertEqual([g["id"] for g in r.get_json()], ["b"])


if __name__ == "__main__":
    unittest.main()
