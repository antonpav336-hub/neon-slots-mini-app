import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/test")
os.environ.setdefault("JWT_SECRET", "test-only-secret")

from pydantic import ValidationError
from server.main import (
    PLINKO_MULTIPLIERS,
    CasinoDepositCreateRequest,
    CasinoWithdrawRequest,
    CrashStartRequest,
    BlackjackStartRequest,
    MinesStartRequest,
    LadderStartRequest,
    PlinkoDropRequest,
    blackjack_score,
    mines_multiplier,
    roulette_bet_wins,
    RouletteBetRequest,
    vip_payload,
    InstantGameRequest,
    baccarat_card_value,
    baccarat_total,
    resolve_baccarat,
)


class CasinoGameLogicTests(unittest.TestCase):
    def test_plinko_payout_table_has_13_symmetric_positive_buckets(self):
        self.assertEqual(len(PLINKO_MULTIPLIERS), 13)
        self.assertEqual(PLINKO_MULTIPLIERS, list(reversed(PLINKO_MULTIPLIERS)))
        self.assertTrue(all(value > 0 for value in PLINKO_MULTIPLIERS))

    def test_plinko_and_all_game_stake_models_accept_one_billion_rubles(self):
        limit = 100_000_000_000  # kopecks
        models = [
            CrashStartRequest, BlackjackStartRequest, MinesStartRequest,
            LadderStartRequest, PlinkoDropRequest,
        ]
        for model in models:
            payload = {"bet_kopecks": limit}
            if model is MinesStartRequest:
                payload["mine_count"] = 3
            if model is PlinkoDropRequest:
                payload["request_key"] = "test_request_key_0001"
            self.assertEqual(model(**payload).bet_kopecks, limit, model.__name__)

    def test_deposit_and_withdraw_accept_one_billion_rubles(self):
        limit = 100_000_000_000
        self.assertEqual(CasinoDepositCreateRequest(amount_kopecks=limit).amount_kopecks, limit)
        self.assertEqual(
            CasinoWithdrawRequest(amount_kopecks=limit, request_key="test_withdraw_key_0001").amount_kopecks,
            limit,
        )

    def test_stake_models_reject_values_above_one_billion_rubles(self):
        with self.assertRaises(ValidationError):
            CrashStartRequest(bet_kopecks=100_000_000_001)

    def test_blackjack_ace_softens_when_total_exceeds_21(self):
        total, soft = blackjack_score([
            {"rank": "A", "suit": "♠"},
            {"rank": "9", "suit": "♥"},
            {"rank": "8", "suit": "♦"},
        ])
        self.assertEqual(total, 18)
        self.assertFalse(soft)

    def test_mines_multiplier_increases_with_safe_picks(self):
        values = [mines_multiplier(3, opened) for opened in range(0, 6)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 1.0)

    def test_roulette_zero_never_wins_red_or_black(self):
        red = RouletteBetRequest(selection="color", value="red", amount_kopecks=100)
        black = RouletteBetRequest(selection="color", value="black", amount_kopecks=100)
        self.assertFalse(roulette_bet_wins(red, 0)[0])
        self.assertFalse(roulette_bet_wins(black, 0)[0])

    def test_new_instant_games_accept_one_billion_virtual_rubles(self):
        limit = 100_000_000_000
        for game, choice in [("dice", "over"), ("hilo", "higher"),
                             ("baccarat", "banker"), ("tower", "5")]:
            request = InstantGameRequest(
                game=game, choice=choice, bet_kopecks=limit,
                request_key="instant_test_key_0001",
            )
            self.assertEqual(request.bet_kopecks, limit)

    def test_new_instant_games_reject_unknown_game(self):
        with self.assertRaises(ValidationError):
            InstantGameRequest(game="slots", choice="over", bet_kopecks=100,
                               request_key="instant_test_key_0002")

    def test_baccarat_card_values_and_modulo_ten(self):
        self.assertEqual(baccarat_card_value(1), 1)
        self.assertEqual(baccarat_card_value(10), 0)
        self.assertEqual(baccarat_card_value(13), 0)
        self.assertEqual(baccarat_total([9, 8]), 7)

    def test_baccarat_resolution_has_valid_totals_and_winner(self):
        for _ in range(100):
            result = resolve_baccarat()
            self.assertIn(result["winner"], ("player", "banker", "tie"))
            self.assertGreaterEqual(result["player_total"], 0)
            self.assertLessEqual(result["player_total"], 9)
            self.assertGreaterEqual(result["banker_total"], 0)
            self.assertLessEqual(result["banker_total"], 9)
            self.assertTrue(2 <= len(result["player_cards"]) <= 3)
            self.assertTrue(2 <= len(result["banker_cards"]) <= 3)

    def test_vip_tier_boundaries(self):
        self.assertEqual(vip_payload(0)["level"], "Bronze")
        self.assertEqual(vip_payload(10_000_000)["level"], "Silver")
        self.assertEqual(vip_payload(100_000_000)["level"], "Gold")
        self.assertEqual(vip_payload(1_000_000_000)["level"], "Diamond")
        self.assertEqual(vip_payload(1_000_000_000)["progress_percent"], 100)


if __name__ == "__main__":
    unittest.main()
