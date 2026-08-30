from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

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

    def test_common_unbraced_math_and_markdown_image_paths_are_normalized(self) -> None:
        rendered = _formatted_text(r"向量 \vec a，系数为 \frac56，角为 $\omega=60^\circ$。![原题图](data/private/image.png)")

        self.assertNotIn("\\vec", rendered)
        self.assertNotIn("\\frac", rendered)
        self.assertNotIn("\\omega", rendered)
        self.assertNotIn("data/private", rendered)
        self.assertIn("【原题图】", rendered)
        self.assertEqual(_replace_math_args(r"\vec a=\frac56,\sqrt3,\omega=60^\circ"), "a⃗=5⁄6,√3,ω=60°")

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

    def test_portable_question_image_is_embedded(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "bank-assets" / ("a" * 64 + ".png")
            asset.parent.mkdir()
            Image.new("RGB", (320, 180), "white").save(asset)
            content = build_practice_pdf(
                [{
                    "kind": "original", "error_id": "image-question", "question_id": None,
                    "stem_text": f"如图。![原题图](bank-assets/{asset.name})\n求证。", "answer_text": None,
                    "difficulty": None, "source_title": "个人错题本", "reason": "错题回顾",
                }],
                include_answers=False,
                asset_root=root,
            )

        self.assertIn(b"/Subtype /Image", content)

    def test_stage_reference_and_redo_layouts_both_build(self) -> None:
        items = [
            {"kind": "original", "error_id": "redo", "question_id": None, "stem_text": "原题甲", "answer_text": None, "difficulty": None, "source_title": "个人错题本", "reason": "第 2 阶段", "review_stage": 2, "requires_original": True},
            {"kind": "recommendation", "error_id": "redo", "question_id": "q1", "stem_text": "推荐题甲", "answer_text": "答案甲", "difficulty": 2, "source_title": "授权题库", "reason": "同类变式"},
            {"kind": "original", "error_id": "reference", "question_id": None, "stem_text": "原题乙", "answer_text": None, "difficulty": None, "source_title": "个人错题本", "reason": "第 4 阶段", "review_stage": 4, "requires_original": False},
            {"kind": "recommendation", "error_id": "reference", "question_id": "q2", "stem_text": "推荐题乙", "answer_text": "答案乙", "difficulty": 3, "source_title": "授权题库", "reason": "迁移训练"},
        ]

        content = build_practice_pdf(items, include_answers=False)

        self.assertTrue(content.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
