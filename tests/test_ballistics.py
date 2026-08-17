import math
import unittest

from wt_bomb_alert import (
    SolverSettings,
    extract_friendly_aircraft,
    extract_safe_zones,
    fall_time,
    find_player,
    solve_release,
)


class BallisticsTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "valid": True,
            "H, m": 1000.0,
            "TAS, km/h": 360.0,
            "IAS, km/h": 340.0,
            "Vy, m/s": 0.0,
        }
        self.indicators = {
            "valid": True,
            "type": "test plane",
            "aviahorizon_roll": 0.0,
            "aviahorizon_pitch": 0.0,
        }
        self.map_info = {"map_min": [0.0, 0.0], "map_max": [10000.0, 10000.0]}
        self.map_objects = [
            {"type": "aircraft", "icon": "Player", "x": 0.1, "y": 0.5, "dx": 1.0, "dy": 0.0}
        ]

    def test_level_fall_time(self):
        self.assertAlmostEqual(fall_time(1000, 0), math.sqrt(2000 / 9.80665), places=6)

    def test_climb_increases_fall_time(self):
        self.assertGreater(fall_time(1000, 20), fall_time(1000, 0))

    def test_player_identification(self):
        self.assertEqual(find_player(self.map_objects)["x"], 0.1)

    def test_solution_on_aligned_run(self):
        solution = solve_release(
            self.state,
            self.indicators,
            self.map_info,
            self.map_objects,
            (0.8, 0.5),
            SolverSettings(horizontal_retention=1.0),
        )
        self.assertEqual(solution["status"], "enroute")
        self.assertAlmostEqual(solution["distance_m"], 7000.0)
        self.assertAlmostEqual(solution["release_distance_m"], 100 * solution["fall_time_s"])
        self.assertAlmostEqual(solution["seconds_to_release"], (7000 - solution["release_distance_m"]) / 100)

    def test_cross_track_guard(self):
        solution = solve_release(
            self.state,
            self.indicators,
            self.map_info,
            self.map_objects,
            (0.8, 0.8),
            SolverSettings(max_cross_track_m=500),
        )
        self.assertEqual(solution["status"], "misaligned")

    def test_target_behind(self):
        solution = solve_release(
            self.state,
            self.indicators,
            self.map_info,
            self.map_objects,
            (0.05, 0.5),
            SolverSettings(),
        )
        self.assertEqual(solution["status"], "not_closing")

    def test_only_fixed_mission_zones_are_exposed(self):
        objects = self.map_objects + [
            {"type": "bombing_point", "color[]": [250, 12, 0], "x": 0.7, "y": 0.4},
            {"type": "airfield", "color[]": [23, 77, 255], "sx": 0.6, "sy": 0.5, "ex": 0.8, "ey": 0.5},
            {"type": "ground_model", "icon": "Tank", "x": 0.4, "y": 0.4},
            {"type": "aircraft", "icon": "Fighter", "x": 0.3, "y": 0.3},
        ]
        zones = extract_safe_zones(objects)
        self.assertEqual([zone["kind"] for zone in zones], ["bombing", "airfield"])
        self.assertEqual(zones[0]["team"], "hostile")
        self.assertAlmostEqual(zones[1]["x"], 0.7)

    def test_only_confirmed_friendly_aircraft_are_exposed(self):
        objects = [
            {"type": "aircraft", "icon": "Player", "color[]": [250, 200, 30], "x": 0.1, "y": 0.1, "dx": 1, "dy": 0},
            {"type": "aircraft", "icon": "Fighter", "color[]": [23, 77, 255], "x": 0.2, "y": 0.2, "dx": 1, "dy": 0},
            {"type": "aircraft", "icon": "Bomber", "color[]": [20, 240, 40], "x": 0.3, "y": 0.3, "dx": 0, "dy": -1},
            {"type": "aircraft", "icon": "Fighter", "color[]": [250, 12, 0], "x": 0.4, "y": 0.4, "dx": -1, "dy": 0},
            {"type": "aircraft", "icon": "Fighter", "x": 0.5, "y": 0.5, "dx": -1, "dy": 0},
        ]
        allies = extract_friendly_aircraft(objects, (0, 0, 10000, 10000))
        self.assertEqual(len(allies), 2)
        self.assertEqual([ally["aircraft_type"] for ally in allies], ["Fighter", "Bomber"])


if __name__ == "__main__":
    unittest.main()
