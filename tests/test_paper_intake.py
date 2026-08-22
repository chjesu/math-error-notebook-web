from __future__ import annotations

import unittest

from services.web_domain import PaperDraft, PaperItem


class PaperIntakeTests(unittest.TestCase):
    def test_split_merge_exclude_and_confirm_are_versioned(self) -> None:
        draft = PaperDraft([PaperItem("q1", "第一题", "答1"), PaperItem("q2", "第二、三题混在一起", "")])
        first, second = draft.split("q2", expected_version=1, second_id="q3", first_text="第二题", second_text="第三题")
        self.assertEqual((first.input_version, second.input_version), (2, 1))
        merged = draft.merge("q2", "q3", first_version=2, second_version=1)
        self.assertIn("第三题", merged.question_text)
        excluded = draft.exclude("q1", expected_version=1)
        self.assertFalse(excluded.included)
        included = draft.confirm({item.item_id: item.input_version for item in draft.items})
        self.assertEqual([item.item_id for item in included], ["q2"])

    def test_stale_or_non_adjacent_edit_fails(self) -> None:
        draft = PaperDraft([PaperItem("q1", "一", ""), PaperItem("q2", "二", ""), PaperItem("q3", "三", "")])
        with self.assertRaisesRegex(RuntimeError, "input_version_changed"):
            draft.revise("q1", expected_version=2, question_text="一", answer_text="")
        with self.assertRaisesRegex(ValueError, "adjacent"):
            draft.merge("q1", "q3", first_version=1, second_version=1)

    def test_confirm_rejects_stale_version_map(self) -> None:
        draft = PaperDraft([PaperItem("q1", "题目", "答案")])
        with self.assertRaisesRegex(RuntimeError, "input_version_changed"):
            draft.confirm({"q1": 2})


if __name__ == "__main__":
    unittest.main()
