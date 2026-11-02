import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from dubkit import core, qa
from dubkit.review import compare_text, placement_review


class ReviewTests(unittest.TestCase):
    def test_slates_fillers_and_repeated_takes_are_not_free(self):
        expected = "The boat has a solar roof"
        for heard in ("Take two "+expected, expected+" um", expected+" "+expected):
            with self.subTest(heard=heard):
                result = compare_text(expected, heard)
                self.assertTrue(result["review"])
                self.assertGreater(result["error_counts"]["insertions"], 0)
                self.assertEqual(result["error_counts"]["deletions"], 0)

    def test_single_missing_negation_and_changed_number_need_review(self):
        missing = compare_text("It does not need a 10 kW motor", "It does need a 10 kW motor")
        self.assertEqual(missing["error_counts"]["deletions"], 1)
        self.assertEqual(missing["differences"][0]["expected"], "not")
        number = compare_text("A 10 kW motor", "A 100 kW motor")
        self.assertEqual(number["error_counts"]["substitutions"], 1)
        self.assertTrue(number["review"])

    def test_equal_words_in_different_order_are_not_equal_speech(self):
        self.assertTrue(compare_text("solar roof electric motor", "electric motor solar roof")["review"])

    def test_punctuation_accents_and_explicit_aliases(self):
        result = compare_text("D’aquest sostre: deu quilowatts.", "d' aquest sostre 10 kW",
                              {"deu quilowatts": "10 kw"})
        self.assertEqual(result["token_error_rate"], 0)
        self.assertFalse(result["review"])

    def test_chinese_character_difference_is_visible(self):
        result = compare_text("这是一艘船", "这是一艘船船")
        self.assertEqual(result["reference_tokens"], 5)
        self.assertEqual(result["error_counts"]["insertions"], 1)

    def test_empty_recognition_and_large_insertions(self):
        self.assertEqual(compare_text("one two", "")["error_counts"]["deletions"], 2)
        self.assertFalse(compare_text("", "")["review"])
        self.assertEqual(compare_text("", "slate")["error_counts"]["insertions"], 1)
        self.assertGreater(compare_text("one", "one two three four")["token_error_rate"], 1)

    def test_report_keeps_source_shift_even_with_deliberately_moved_anchor(self):
        source = [{"id": "001", "start": 1., "end": 3.}]
        edits = {"cues": [{"id": "001", "anchor": 1.7}]}
        placements = [{"id": "001", "output_start": 1.7, "output_end": 4., "tempo": 1.2}]
        result = placement_review(source, edits, placements)
        check = result["cue_checks"][0]
        self.assertAlmostEqual(check["source_start_shift_seconds"], .7)
        self.assertEqual(check["anchor_shift_seconds"], 0)
        self.assertEqual(check["review_reasons"], ["source_start_shift", "source_end_overrun", "tempo_change"])
        self.assertEqual(result["flagged_cues"], 1)

    def test_human_block_can_fit_but_shift_its_inner_cue(self):
        source = [{"id": "001", "start": 2., "end": 4.}]
        edits = {"cues": [{"id": "001", "anchor": 2.}]}
        placements = [{"id": "001", "output_start": 2.6, "output_end": 4.1, "tempo": 1.}]
        reasons = placement_review(source, edits, placements)["cue_checks"][0]["review_reasons"]
        self.assertEqual(reasons, ["source_start_shift", "edit_anchor_shift"])

    def test_shorter_translation_may_leave_a_natural_pause(self):
        source = [{"id": "001", "start": 1., "end": 5.}]
        edits = {"cues": [{"id": "001", "anchor": 1.}]}
        placements = [{"id": "001", "output_start": 1., "output_end": 3., "tempo": 1.}]
        self.assertEqual(placement_review(source, edits, placements)["flagged_cues"], 0)

    def test_stale_quality_version_and_incomplete_coverage_are_refused(self):
        render = {"placements": [{"id": "001"}]}
        report = {"version": qa.QA_VERSION, "render_digest": core.digest(render),
                  "complete": True, "covered_cue_ids": ["001"]}
        with patch("dubkit.qa.lang_dir", return_value=Path('/unused')), patch("dubkit.qa.load", return_value=report):
            self.assertEqual(qa.require_qa('/unused', 'ca-ES', render), report)
            for field, value in (("version", 1), ("render_digest", "old"), ("complete", False), ("covered_cue_ids", [])):
                changed = deepcopy(report); changed[field] = value
                with self.subTest(field=field), patch("dubkit.qa.load", return_value=changed):
                    with self.assertRaises(ValueError):
                        qa.require_qa('/unused', 'ca-ES', render)


if __name__ == "__main__":
    unittest.main()
