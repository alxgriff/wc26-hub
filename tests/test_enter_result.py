"""Unit tests for scripts/enter_result.py.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import enter_result as er

HEADER = ("match_id,group,matchday,date_et,kickoff_et_24h,kickoff_et,team_a,team_b,"
          "stadium,city,country,tv_us,score_a,score_b,status,notes")
A2 = "A2,A,1,2026-06-11,22:00,10:00 PM,South Korea,Czechia,Estadio Akron,Guadalajara,Mexico,FS1,,,scheduled,"
# a row whose notes contain a comma → the csv module MUST keep it quoted on round-trip
A5 = ('A5,A,3,2026-06-24,21:00,9:00 PM,Czechia,Mexico,Estadio Azteca,Mexico City,Mexico,Fox,,,'
      'scheduled,"Simultaneous with A6, matchday 3 rule"')


def lines(*rows):
    return [HEADER, *rows]


class ParseScoreTests(unittest.TestCase):
    def test_valid_hyphen_and_en_dash(self):
        self.assertEqual(er.parse_score("2-1"), (2, 1))
        self.assertEqual(er.parse_score(" 0 - 0 "), (0, 0))
        self.assertEqual(er.parse_score("3–2"), (3, 2))   # en dash

    def test_invalid(self):
        for bad in ("2:1", "x-1", "2", "-1-0", ""):
            with self.assertRaises(er.ResultError):
                er.parse_score(bad)


class ApplyResultTests(unittest.TestCase):
    def test_sets_score_and_status_and_preserves_other_rows(self):
        src = lines(A2, A5)
        out, msg = er.apply_result(src, "A2", 2, 2)
        self.assertEqual(out[0], HEADER)          # header untouched
        self.assertEqual(out[2], A5)              # the row we didn't touch is byte-identical
        self.assertIn("A2,A,1,", out[1])
        # score + status updated, all 16 columns intact
        fields = out[1].split(",")
        self.assertEqual(fields[12], "2")         # score_a
        self.assertEqual(fields[13], "2")         # score_b
        self.assertEqual(fields[14], "played")    # status
        self.assertEqual(fields[6], "South Korea")
        self.assertIn("South Korea 2–2 Czechia", msg)

    def test_refuses_overwrite_without_force(self):
        played, _ = er.apply_result(lines(A2), "A2", 1, 0)
        with self.assertRaises(er.ResultError) as ctx:
            er.apply_result(played, "A2", 3, 3)
        self.assertIn("already played", str(ctx.exception))

    def test_force_overwrites(self):
        played, _ = er.apply_result(lines(A2), "A2", 1, 0)
        out, _ = er.apply_result(played, "A2", 3, 3, force=True)
        self.assertEqual(out[1].split(",")[12:15], ["3", "3", "played"])

    def test_unknown_match_id(self):
        with self.assertRaises(er.ResultError):
            er.apply_result(lines(A2), "B3", 0, 0)

    def test_bad_match_id_format(self):
        with self.assertRaises(er.ResultError):
            er.apply_result(lines(A2), "ZZ", 0, 0)

    def test_quoted_notes_row_round_trips_when_edited(self):
        # editing A5 itself must keep its comma-bearing notes correctly quoted
        out, _ = er.apply_result(lines(A5), "A5", 0, 1)
        self.assertIn('"Simultaneous with A6, matchday 3 rule"', out[1])
        self.assertEqual(out[1].split(",")[14], "played")


class FileRoundTripTests(unittest.TestCase):
    def test_cli_write_reads_back_with_bom_safe_encoding(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fixtures.csv"
            path.write_text("\n".join(lines(A2, A5)) + "\n", encoding="utf-8-sig")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = er.main(["A2", "1-0", "--fixtures", str(path)])
            self.assertEqual(rc, 0)
            back = er._read_lines(path)
            self.assertEqual(back[1].split(",")[12:15], ["1", "0", "played"])
            self.assertEqual(back[2], A5)         # other row preserved


if __name__ == "__main__":
    unittest.main()
