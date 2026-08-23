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
        self.assertIn('localStorage.getItem("lzlm-device-id")', script)
        self.assertIn('"X-Device-ID": deviceId', script)

    def test_mobile_layout_and_keyboard_focus_are_defined(self) -> None:
        css = (Path(__file__).resolve().parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:720px)", css)
        self.assertIn(":focus-visible", css)
        self.assertNotIn("min-width:720px", css)

    def test_auth_flow_handles_captcha_retry_and_browser_history(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="captcha-token"', html)
        self.assertIn("local-captcha", html)
        self.assertIn("captcha_required", script)
        self.assertIn("error.retryAfter", script)
        self.assertIn("countdown > 0", script)
        self.assertIn("function routePage()", script)
        self.assertIn("showLegal", script)
        self.assertIn('$("#password").type = "password"', script)
        self.assertIn("resetOtp()", script)
        self.assertIn('challenge = result.challenge_token;\n    const localTestCode =', script)
        self.assertIn('$("#code").value = localTestCode;', script)
        self.assertIn('$("#agreement-fields").hidden = false', script)
        self.assertIn('$("#agreement").addEventListener("change", refreshAuthControls)', script)
        self.assertNotIn('authMode === "register" && !$("#agreement").checked', script)
        self.assertIn("let authRevision = 0", script)
        self.assertIn("requestRevision !== authRevision", script)
        self.assertIn('$("#login-tab").disabled = busy', script)
        self.assertIn('$("#phone").disabled = busy', script)
        self.assertIn("result.local_test_code", script)
        self.assertIn("仅限本地测试：模拟验证码已自动填入。", script)
        self.assertIn("仅限本地测试：操作验证码已自动填入。", script)

    def test_auth_first_screen_and_control_states_match_ux_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        css = (root / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('<form id="code-form">', html)
        self.assertIn('id="code"', html)
        self.assertIn('id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required disabled', html)
        self.assertIn('id="otp-button" disabled', html)
        self.assertIn('id="auth-submit" disabled', html)
        self.assertIn('id="password" type="password"', html)
        self.assertIn('minlength="8" maxlength="20"', html)
        self.assertIn('/[A-Za-z]/.test(value)', script)
        self.assertIn('/[0-9]/.test(value)', script)
        self.assertIn('function refreshAuthControls()', script)
        self.assertIn('function validPhone()', script)
        self.assertIn('function validCode()', script)
        self.assertIn('function validPassword()', script)
        self.assertIn('$("#code").disabled = !challenge', script)
        self.assertIn('$("#auth-submit").disabled = !challenge || !phoneIsValid || !validCode() || !$("#agreement").checked || authSubmitting;', script)
        self.assertNotIn('!passwordIsValid || authSubmitting', script)
        self.assertIn('$("#phone").addEventListener("input"', script)
        self.assertIn('$("#code").addEventListener("input"', script)
        self.assertIn('$("#password").addEventListener("input", refreshAuthControls)', script)
        self.assertIn('$("#agreement").addEventListener("change", refreshAuthControls)', script)
        self.assertNotIn('$("#code-form").hidden =', script)
        self.assertIn('const legalActive = location.pathname.startsWith("/legal/")', script)
        self.assertIn('if (location.pathname.startsWith("/legal/")) return;', script)
        self.assertIn('window.addEventListener("popstate", () => { routePage(); refresh(); })', script)
        for token in ("#002060", "#F6F5F1", "#182230", "#586474", "#D9DEE7"):
            self.assertIn(token, css)


if __name__ == "__main__":
    unittest.main()
