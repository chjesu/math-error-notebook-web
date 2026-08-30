from __future__ import annotations

import unittest

from services.web_domain.practice_pdf import _formatted_text, _replace_math_args, build_practice_pdf


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

    def test_dense_inline_math_can_build_a_pdf(self) -> None:
        content = build_practice_pdf(
            [{
                "kind": "original",
                "error_id": "error-dense-math",
                "question_id": None,
                "stem_text": r"如图，在菱形 $ABCD$ 中，$AB=2$，$\angle BAD=60^\circ$，求 $\overrightarrow{AN}\cdot\overrightarrow{MN}$ 的范围。",
                "answer_text": None,
                "difficulty": None,
                "source_title": "个人错题本",
                "reason": "错题回顾",
            }],
            include_answers=False,
        )

        self.assertTrue(content.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
