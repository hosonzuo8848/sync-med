# -*- coding: utf-8 -*-
"""A one-seat council with a live D1 gap does not spend that seat on a repo."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "council"))

from council import intel_seat_count  # noqa: E402


class Seats(unittest.TestCase):
    def test_one_seat_with_a_gap_stays_on_the_gap(self):
        self.assertEqual(intel_seat_count(1, 5, True), 0)

    def test_one_seat_without_a_gap_can_be_radar(self):
        self.assertEqual(intel_seat_count(1, 5, False), 1)

    def test_three_seats_keep_one_for_the_gap_and_a_radar_majority(self):
        self.assertEqual(intel_seat_count(3, 5, True), 2)

    def test_empty_radar_takes_nothing(self):
        self.assertEqual(intel_seat_count(3, 0, True), 0)


if __name__ == "__main__":
    unittest.main()
