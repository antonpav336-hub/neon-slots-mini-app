import unittest

from server.extended_casino import CASES, MAX_BET, MAX_PAYOUT, SYMBOLS


class ExtendedCasinoConfigurationTests(unittest.TestCase):
    def test_all_case_odds_sum_to_one(self):
        for case_id, case in CASES.items():
            self.assertAlmostEqual(sum(chance for chance, _ in case["items"]), 1.0, places=9)
            self.assertGreater(case["price"], 0)

    def test_case_prizes_are_positive_and_tiered(self):
        for case in CASES.values():
            payouts = [payout for _, payout in case["items"]]
            self.assertTrue(all(value > 0 for value in payouts))
            self.assertEqual(payouts, sorted(payouts))

    def test_slot_symbols_have_unique_ids_and_positive_weights(self):
        ids = [item["id"] for item in SYMBOLS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item["weight"] > 0 for item in SYMBOLS))

    def test_limits_match_one_billion_virtual_rubles(self):
        self.assertEqual(MAX_BET, 100_000_000_000)
        self.assertGreater(MAX_PAYOUT, MAX_BET)


if __name__ == "__main__":
    unittest.main()
