from __future__ import annotations

import unittest
from io import BytesIO

from pypdf import PdfReader

from services.web_domain.practice_pdf import _formatted_text, _replace_math_args, build_practice_pdf, practice_mode


class PracticePdfMathTests(unittest.TestCase):
    def test_latex_is_rendered_without_leaking_control_sequences(self) -> None:
        rendered = _formatted_text(
            r"已知 $\vec{e_1},\vec{e_2}$ 且 $\angle(\vec{e_1},\vec{e_2})=\frac{\pi}{3}$。"
        )
        self.assertIn("<img", rendered)
        self.assertNotIn(r"\vec", rendered)
        self.assertNotIn(r"\angle", rendered)
        self.assertNotIn(r"\frac", rendered)
        self.assertNotIn("\\", rendered)

    def test_unicode_inner_product_brackets_do_not_force_plain_text_fallback(self) -> None:
        rendered = _formatted_text(
            r"已知 $\vec{e_{1}},\vec{e_{2}}$ 且 $\left⟨\vec{e_{1}},\vec{e_{2}}\right⟩=\frac{\pi}{3}$。"
        )
        self.assertGreaterEqual(rendered.count("<img"), 2)
        self.assertNotIn(r"\vec", rendered)
        self.assertNotIn(r"\frac", rendered)
        self.assertEqual(_replace_math_args(r"\vec{e_{1}}"), "e⃗₁")
        self.assertEqual(
            _replace_math_args(r"\left⟨\vec{e_{1}},\vec{e_{2}}\right⟩=\frac{\pi }{3}"),
            "⟨e⃗₁,e⃗₂⟩=π⁄3",
        )

    def test_operator_vector_text_is_normalized(self) -> None:
        rendered = _formatted_text(r"且 \operatorname{vec}(e_1),\operatorname{vec}(e_2)=0")
        self.assertIn("<img", rendered)
        self.assertNotIn(r"\operatorname", rendered)

    def test_plain_text_is_not_rewritten_as_missing_subscript_glyphs(self) -> None:
        rendered = _formatted_text("学生填写 e_1，但没有使用公式。")
        self.assertIn("e_1", rendered)

    def test_review_and_self_test_templates_do_not_leak_the_same_content(self) -> None:
        items = [
            {
                "kind": "original",
                "error_id": "e" * 32,
                "question_id": None,
                "stem_text": "解方程 x+1=2",
                "answer_text": "x=0",
                "cause_label": "代数变形错误",
                "knowledge_points": ["一元一次方程"],
                "correct_solution": "两边同时减一",
                "final_answer": "x=1",
                "prevention_cue": "移项后检查符号",
                "difficulty": 2.0,
                "source_title": "个人错题本",
                "reason": "错题回顾",
            },
            {
                "kind": "recommendation",
                "error_id": "e" * 32,
                "question_id": "q" * 32,
                "stem_text": "解方程 x+2=4",
                "answer_text": "x=2",
                "difficulty": 3.0,
                "source_title": "授权题库",
                "reason": "针对代数变形错误：难度 2→3，递进训练",
            },
        ]

        review = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(build_practice_pdf(items, mode="review"))).pages)
        self_test = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(build_practice_pdf(items, mode="self_test"))).pages)

        self.assertIn("复习卷（含解析）", review)
        self.assertIn("代数变形错误", review)
        self.assertIn("两边同时减一", review)
        self.assertIn("x=2", review)
        self.assertIn("巩固自测卷", self_test)
        self.assertIn("作答区", self_test)
        self.assertNotIn("代数变形错误", self_test)
        self.assertNotIn("x=2", self_test)

    def test_practice_mode_normalizes_legacy_boolean_and_rejects_conflicts(self) -> None:
        self.assertEqual(practice_mode(None, None), "self_test")
        self.assertEqual(practice_mode(None, True), "review")
        self.assertEqual(practice_mode("self_test", False), "self_test")
        with self.assertRaisesRegex(ValueError, "conflicting practice mode"):
            practice_mode("review", False)
        with self.assertRaisesRegex(ValueError, "invalid practice mode"):
            practice_mode("answers", None)


if __name__ == "__main__":
    unittest.main()
