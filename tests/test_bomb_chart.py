import unittest

from wt_bomb_alert import (
    match_aircraft_br,
    parse_aircraft_sheet_csv,
    parse_bomb_chart_csv,
    parse_mass_to_kg,
)


class BombChartTests(unittest.TestCase):
    def test_mass_parser_supports_kg_and_lb(self):
        self.assertEqual(parse_mass_to_kg("401 kg"), 401.0)
        self.assertAlmostEqual(parse_mass_to_kg("500 lb"), 226.796185, places=5)

    def test_bomb_counts_and_merged_variant_inheritance(self):
        content = (
            ",,,227 kg,89 kg,Mk 82,Mk 82,Bomb,2,3,4,5,6,7,,2464\n"
            ",,,227 kg,89 kg,Mk 82,GBU-12 Paveway II,Guided Bomb,,,,,,,,2464\n"
        )
        bombs = parse_bomb_chart_csv(content)
        self.assertEqual(len(bombs), 2)
        self.assertEqual(bombs[0]["counts"], [2, 3, 4, 5, 6, 7])
        self.assertEqual(bombs[1]["counts"], [2, 3, 4, 5, 6, 7])

    def test_internal_aircraft_name_matches_sheet_br(self):
        content = (
            ',,,,"F-104S\n11.3",,"BLU-1 × 2"\n'
            ',,,,,,"BLU-1 × 2",,"Mk 84 × 1"\n'
        )
        catalog = parse_aircraft_sheet_csv(content, "Italy")
        match = match_aircraft_br("f-104s_cb", catalog)
        self.assertIsNotNone(match)
        self.assertEqual(match["name"], "F-104S")
        self.assertEqual(match["br"], 11.3)
        self.assertEqual(match["bomb_names"], ["BLU-1", "Mk 84"])


if __name__ == "__main__":
    unittest.main()
