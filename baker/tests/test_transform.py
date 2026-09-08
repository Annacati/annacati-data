"""Unit tests for the write-back transform. The pure apply_transforms path needs
no lua; the one end-to-end compute_transforms test skips when no `lua` on PATH
(mirrors annacati-pipeline/tests/test_lua_preview.py)."""
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transform  # noqa: E402


class TestColour(unittest.TestCase):
    def test_parse_and_hex_roundtrip(self):
        self.assertEqual(transform.parse_color("1C004F"), 0x1C004F)
        self.assertEqual(transform.parse_color("#1C004F"), 0x1C004F)
        self.assertEqual(transform.parse_color(""), 0)
        self.assertEqual(transform.parse_color("bad"), 0)
        self.assertEqual(transform.color_hex6(0x1C004F), "1C004F")
        self.assertEqual(transform.color_hex6(0), "")

    def test_coord_format(self):
        self.assertEqual(transform._fmt_coord(37.0), "37")
        self.assertEqual(transform._fmt_coord(37.5012294), "37.501229")


class TestApplyTransforms(unittest.TestCase):
    def _tables(self):
        return {
            "agency.txt": [{"agency_id": "a1", "agency_name": "ETNA TRASPORTI"}],
            "routes.txt": [{"route_id": "r1", "route_short_name": "101",
                            "route_color": "", "route_text_color": "", "route_type": "3"}],
            "stops.txt": [{"stop_id": "s1", "stop_name": "CATANIA CENTRO",
                           "stop_lat": "37.5", "stop_lon": "15.0"}],
            "trips.txt": [{"trip_id": "t1", "trip_headsign": "CATANIA"},
                          {"trip_id": "t2", "trip_headsign": "CATANIA"}],
        }

    def test_writes_back_all_fields(self):
        tables = self._tables()
        mut = {
            "routes": {"r1": {"color": 0x1C004F, "text_color": 0xFFFFFF,
                              "short_name": "101", "route_type": 200}},
            "stops": {"s1": {"name": "Catania Centro", "description": "",
                             "lat": 37.501229, "lng": 15.088805,
                             "names": [["en", "Catania Downtown"]]}},
            "headsigns": {"CATANIA": "Catania"},
            "agencies": {"a1": {"name": "Etna Trasporti"}},
        }
        summary = transform.apply_transforms(tables, mut)
        self.assertEqual(tables["routes.txt"][0]["route_color"], "1C004F")
        self.assertEqual(tables["routes.txt"][0]["route_text_color"], "FFFFFF")
        self.assertEqual(tables["routes.txt"][0]["route_type"], "200")
        self.assertEqual(tables["stops.txt"][0]["stop_name"], "Catania Centro")
        self.assertEqual(tables["stops.txt"][0]["stop_lat"], "37.501229")
        self.assertEqual(tables["agency.txt"][0]["agency_name"], "Etna Trasporti")
        # both trips with the same old headsign are rewritten
        self.assertTrue(all(t["trip_headsign"] == "Catania" for t in tables["trips.txt"]))
        self.assertEqual(summary["headsigns_changed"], 2)
        self.assertEqual(summary["translations"], 1)
        # translations.txt table emitted (it, the default, excluded)
        tr = tables["translations.txt"]
        self.assertEqual(tr, [{"table_name": "stops", "field_name": "stop_name",
                               "language": "en", "translation": "Catania Downtown",
                               "record_id": "s1"}])

    def test_never_blanks_a_shipped_colour(self):
        tables = self._tables()
        tables["routes.txt"][0]["route_color"] = "AABBCC"
        # script produced no colour (0) -> keep the shipped one
        mut = {"routes": {"r1": {"color": 0, "text_color": 0,
                                 "short_name": "101", "route_type": 3}},
               "stops": {}, "headsigns": {}, "agencies": {}}
        transform.apply_transforms(tables, mut)
        self.assertEqual(tables["routes.txt"][0]["route_color"], "AABBCC")

    def test_empty_mutations_are_noop(self):
        tables = self._tables()
        summary = transform.apply_transforms(
            tables, {"routes": {}, "stops": {}, "headsigns": {}, "agencies": {}})
        self.assertEqual(sum(summary.values()), 0)
        self.assertNotIn("translations.txt", tables)


@unittest.skipUnless(shutil.which("lua"), "no lua interpreter on PATH")
class TestComputeEndToEnd(unittest.TestCase):
    def test_runs_composed_lua(self):
        # A minimal composed script: colour every route, upper->title one stop.
        composed = r"""
        function process_route(r) r:set_color(0x1C004F); r:set_text_color(0xFFFFFF) end
        function process_agency(a) a:set_name("Etna Trasporti") end
        """
        tables = {
            "agency.txt": [{"agency_id": "a1", "agency_name": "ETNA TRASPORTI"}],
            "routes.txt": [{"route_id": "r1", "route_short_name": "101",
                            "route_color": "", "route_text_color": "", "route_type": "3"}],
            "stops.txt": [], "trips.txt": [],
        }
        mut = transform.compute_transforms(tables, composed)
        self.assertNotIn("error", mut)
        self.assertEqual(mut["routes"]["r1"]["color"], 0x1C004F)
        self.assertEqual(mut["agencies"]["a1"]["name"], "Etna Trasporti")
        # empty entity maps coerced back to dicts (not [])
        self.assertIsInstance(mut["stops"], dict)
        self.assertIsInstance(mut["headsigns"], dict)


if __name__ == "__main__":
    unittest.main()
