from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import io
import pypdf
from PIL import Image

from services.web_domain.practice_pdf import _formatted_text, _normalize_math_text, _replace_math_args, _render_math_image, build_practice_pdf


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

    def test_standard_vector_fraction_and_radical_layout_is_rendered(self) -> None:
        normalized = _normalize_math_text(
            r"\overrightarrow{AM}=\frac56\overrightarrow{AB}+\frac12\overrightarrow{AD},\quad |\varphi|<\pi/2,\quad \sqrt{3}"
        )

        self.assertIn(r"\frac{5}{6}", normalized)
        self.assertIn(r"\frac{1}{2}", normalized)
        self.assertIn(r"\frac{\pi}{2}", normalized)
        rendered = _render_math_image(normalized, 11)
        self.assertIsNotNone(rendered)
        self.assertGreater(rendered[2], 11)

    def test_plain_text_is_not_rewritten_as_missing_subscript_glyphs(self) -> None:
        rendered = _formatted_text("学生填写 e_1，但没有使用公式。")
        self.assertIn("e_1", rendered)

    def test_tex_shorthand_is_rendered_without_corrupting_commands(self) -> None:
        examples = {
            r"x\in[-\frac\pi6,\frac\pi{12}]": r"x\in[-\frac{\pi}{6},\frac{\pi}{12}]",
            r"e=\frac2{\sqrt5}": r"e=\frac{2}{\sqrt{5}}",
            r"e=\frac{\sqrt{10}}5": r"e=\frac{\sqrt{10}}{5}",
            r"\sqrt3a": r"\sqrt{3}a",
            r"\sqrt[3]8": r"\sqrt[3]{8}",
            r"x\in\mathbb R,0\le\varphi<2\pi,n\ge3": r"x\in\mathbb{R},0\leq\varphi<2\pi,n\geq3",
            r"A_n=\{1,2,\cdots,n\}(n\in\mathbb N^*)": r"A_n=\{1,2,\cdots,n\}(n\in\mathbb{N}^*)",
            r"\frac{\frac12}{\sqrt3}": r"\frac{\frac{1}{2}}{\sqrt{3}}",
            r"x\ne0，y：2": r"x\neq0,y:2",
        }
        for source, expected in examples.items():
            with self.subTest(source=source):
                self.assertEqual(_normalize_math_text(source), expected)
                self.assertIsNotNone(_render_math_image(source, 11))

    def test_fallback_replaces_whole_commands_not_prefixes(self) -> None:
        self.assertEqual(_replace_math_args(r"\{1,2,\cdots,n\}\rightarrow\mathbb R"), "{1,2,⋯,n}→R")
        self.assertEqual(_replace_math_args(r"0\le\varphi<\frac\pi{12}"), "0≤φ<π⁄12")

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

    def test_brand_logo_is_embedded(self) -> None:
        with TemporaryDirectory() as temporary:
            logo = Path(temporary) / "logo.png"
            Image.new("RGBA", (128, 128), (23, 92, 211, 255)).save(logo)
            content = build_practice_pdf(
                [{
                    "kind": "original", "error_id": "logo-question", "question_id": None,
                    "stem_text": "求一元二次方程的根。", "answer_text": None,
                    "difficulty": None, "source_title": "个人错题本", "reason": "错题回顾",
                }],
                include_answers=False,
                logo_path=logo,
            )

        self.assertIn(b"/Subtype /Image", content)

    def test_upload_photo_is_omitted_but_recommendation_diagram_is_kept(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            upload = root / "student-upload.png"
            Image.new("RGB", (400, 600), "red").save(upload)
            asset = root / "bank-assets" / ("b" * 64 + ".png")
            asset.parent.mkdir()
            Image.new("RGB", (320, 180), "blue").save(asset)
            original = {"kind": "original", "error_id": "a" * 32, "stem_text": "原题题干",
                        "error_reason": "遗漏分类讨论", "image_object_key": upload.name}
            recommendation = {"kind": "recommendation", "error_id": "a" * 32,
                              "question_id": "c" * 32, "stem_text": f"推荐题。![示意图](bank-assets/{asset.name})",
                              "answer_text": "参考答案", "reason": "同类练习", "source_title": "授权题库"}
            for include_answers in (False, True):
                for stage in (1, 4):
                    with self.subTest(include_answers=include_answers, stage=stage):
                        original.update(review_stage=stage, requires_original=stage == 1)
                        without_diagram = build_practice_pdf([original], include_answers=include_answers, asset_root=root)
                        self.assertNotIn(b"/Subtype /Image", without_diagram)
                        with_diagram = build_practice_pdf([original, recommendation], include_answers=include_answers, asset_root=root)
                        self.assertEqual(with_diagram.count(b"/Subtype /Image"), 1)
            self.assertTrue(upload.is_file())

    def test_stage_reference_and_redo_layouts_both_build(self) -> None:
        items = [
            {"kind": "original", "error_id": "redo", "question_id": None, "stem_text": "原题甲", "answer_text": None, "difficulty": None, "source_title": "个人错题本", "reason": "第 2 阶段", "review_stage": 2, "requires_original": True},
            {"kind": "recommendation", "error_id": "redo", "question_id": "q1", "stem_text": "推荐题甲", "answer_text": "答案甲", "difficulty": 2, "source_title": "授权题库", "reason": "同类变式"},
            {"kind": "original", "error_id": "reference", "question_id": None, "stem_text": "原题乙", "answer_text": None, "difficulty": None, "source_title": "个人错题本", "reason": "第 4 阶段", "review_stage": 4, "requires_original": False},
            {"kind": "recommendation", "error_id": "reference", "question_id": "q2", "stem_text": "推荐题乙", "answer_text": "答案乙", "difficulty": 3, "source_title": "授权题库", "reason": "迁移训练"},
        ]

        content = build_practice_pdf(items, include_answers=False)

        self.assertTrue(content.startswith(b"%PDF-"))

    def test_recommendation_options_are_printed_in_the_pdf(self) -> None:
        items = [
            {"kind": "original", "error_id": "options-original", "question_id": None, "stem_text": "原题", "answer_text": None, "difficulty": None, "source_title": "个人错题本", "reason": "第 1 阶段", "review_stage": 1, "requires_original": True},
            {"kind": "recommendation", "error_id": "options-original", "question_id": "opt-q", "stem_text": "若双曲线 $x^2+\\frac{y^2}{k}=1$ 的离心率是 2，则实数 $k$ 的值是（ ）", "answer_text": "A", "difficulty": 2, "source_title": "授权题库", "reason": "同类变式", "options": ("A．$-3$", "B．$-\\frac{1}{3}$", "C．3", "D．$\\frac{1}{3}$")},
        ]

        content = build_practice_pdf(items, include_answers=True)

        self.assertTrue(content.startswith(b"%PDF-"))
        text = pypdf.PdfReader(io.BytesIO(content)).pages[0].extract_text()
        self.assertIn("选项", text)
        self.assertIn("A．", text)
        self.assertIn("B．", text)
        self.assertIn("C．", text)
        self.assertIn("D．", text)


if __name__ == "__main__":
    unittest.main()
