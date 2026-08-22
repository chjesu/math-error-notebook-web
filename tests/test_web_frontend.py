from pathlib import Path
import unittest


class FrontendContractTests(unittest.TestCase):
    def test_shell_uses_formal_brand_and_has_no_profile_step(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/assets/branding/logo-symbol-color-64-v1.png", html)
        self.assertIn("李兆霖数学错题本", html)
        for removed in ("昵称", "年级", "出生日期", "监护人", "家庭"):
            self.assertNotIn(removed, html + script)
        self.assertIn("next_action", (root / "openapi" / "web-v1.json").read_text(encoding="utf-8"))
        self.assertIn("今日复习", html)
        self.assertIn("练习 PDF", html)
        self.assertIn("/v1/reviews/today", script)
        self.assertIn("/v1/practice-pdfs", script)
        self.assertIn("/manual-candidate", script)
        self.assertIn("/manual-grade", script)
        self.assertIn("确认写入错题本", html)

    def test_mobile_layout_and_keyboard_focus_are_defined(self) -> None:
        css = (Path(__file__).resolve().parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:720px)", css)
        self.assertIn(":focus-visible", css)
        self.assertNotIn("min-width:720px", css)


if __name__ == "__main__":
    unittest.main()
