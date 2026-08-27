import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class FrontendContractTests(unittest.TestCase):
    def test_product_pages_remain_independent_documents(self) -> None:
        pages = {
            "errors.html": ('data-page="errors"', 'id="all-errors"'),
            "reviews.html": ('data-page="reviews"', 'id="review-actions"'),
            "practice.html": ('data-page="practice"', 'id="practice-errors"'),
            "progress.html": ('data-page="progress"', 'id="progress-stats"'),
            "settings.html": ('data-page="settings"', 'id="sensitive-form"'),
        }
        unique_markers = [marker for _, marker in pages.values()]
        for filename, (page_marker, own_marker) in pages.items():
            html = (WEB / filename).read_text(encoding="utf-8")
            self.assertIn(page_marker, html)
            self.assertIn(own_marker, html)
            self.assertIn('/assets/branding/logo-symbol-color-64-v1.png', html)
            self.assertIn('/web/vendor/katex/katex.min.js', html)
            self.assertIn('/web/vendor/katex/auto-render.min.js', html)
            self.assertIn('李兆霖数学错题本', html)
            for route in ('href="/"', 'href="/errors"', 'href="/reviews"', 'href="/practice"', 'href="/progress"', 'href="/settings"'):
                self.assertIn(route, html)
            for icon in ("workbench", "errors", "reviews", "practice", "progress", "settings"):
                self.assertIn(f'/web/nav-icons.svg#{icon}', html)
            self.assertNotIn('href="#', html)
            for other_marker in unique_markers:
                if other_marker != own_marker:
                    self.assertNotIn(other_marker, html)

    def test_workbench_is_the_official_deepseek_harness_surface(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        plugin = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "client.js").read_text(encoding="utf-8")
        host_plugin = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").read_text(encoding="utf-8")
        patch = (ROOT / "config" / "deepseek-harness" / "web-product.patch.yml").read_text(encoding="utf-8")
        preset = (ROOT / "config" / "deepseek-harness" / "agent-presets" / "math-notebook" / "agent.cordis.yml").read_text(encoding="utf-8")
        dependencies = package["dependencies"]
        self.assertEqual(dependencies["@deepseek-ai/dsh"], "0.1.1-rc.2")
        self.assertEqual(dependencies["@deepseek-ai/dsh-web-frontend"], "0.1.1-rc.2")
        self.assertEqual(dependencies["@lizhaolin/dsh-math-notebook-ui"], "file:extensions/dsh-math-notebook-ui")
        self.assertIn('data-page="harness-workbench"', html)
        self.assertIn('id="harness-frame"', html)
        self.assertIn('allow="clipboard-read; clipboard-write"', html)
        self.assertIn('fetch("/v1/session"', html)
        self.assertIn('location.replace("/login")', html)
        self.assertIn('frame.src = "http://127.0.0.1:3080/"', html)
        self.assertNotIn('/web/app.js', html)
        self.assertNotIn('id="upload-form"', html)
        self.assertIn('李兆霖数学错题本', plugin)
        self.assertIn('id: "math-notebook-navigation"', plugin)
        self.assertIn('sidebar.footer.action', plugin)
        for label, route in (("错题本", "/errors"), ("今日复习", "/reviews"), ("练习 PDF", "/practice"), ("学习进度", "/progress"), ("设置与隐私", "/settings")):
            self.assertIn(f'path: "{route}", label: "{label}"', plugin)
        self.assertNotIn('path: "/", label: "工作台"', plugin)
        self.assertIn('aria-label": "错题本功能导航"', plugin)
        self.assertIn('button[aria-label="选择工作区"]', plugin)
        self.assertIn('button[aria-label="添加工作区"]', plugin)
        self.assertIn('item.title === productWorkspaceTitle', plugin)
        self.assertIn('ctx.workspaces.connectWorkspace(workspace.workspaceId)', plugin)
        self.assertIn('ctx.sessions.open(sessionId)', plugin)
        self.assertIn('ctx.workspaceRegistry.create(workspacePath, "错题会话")', host_plugin)
        self.assertIn('LZLM_HARNESS_WORKSPACE_ROOT', host_plugin)
        self.assertIn("ui-brand-official", patch)
        self.assertIn("ui-math-notebook", patch)
        self.assertIn("tool-bash", patch)
        self.assertIn("tool-pwsh", patch)
        self.assertIn("default: math-notebook", patch)
        self.assertIn("includeUserRoot: true", patch)
        self.assertIn("@deepseek-ai/dsh-persona", preset)
        self.assertNotIn("dsh-tool-", preset)

    def test_brand_and_learning_contract_survive_harness_adoption(self) -> None:
        html = "".join(path.read_text(encoding="utf-8") for path in WEB.glob("*.html"))
        script = (WEB / "app.js").read_text(encoding="utf-8")
        for removed in ("昵称", "年级", "出生日期", "监护人", "家庭"):
            self.assertNotIn(removed, html + script)
        self.assertIn("next_action", (ROOT / "openapi" / "web-v1.json").read_text(encoding="utf-8"))
        self.assertIn("今日复习", html)
        self.assertIn("练习 PDF", html)
        self.assertIn("/v1/reviews/today", script)
        self.assertIn("/v1/practice-pdfs", script)
        self.assertIn("/chat-turn", script)
        self.assertIn("await commitCurrent()", script)
        self.assertIn('localStorage.getItem("lzlm-device-id")', script)
        self.assertIn('"X-Device-ID": deviceId', script)

    def test_mobile_layout_and_keyboard_focus_are_defined_for_product_pages(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:720px)", css)
        self.assertIn(":focus-visible", css)
        self.assertNotIn("min-width:720px", css)
        self.assertIn(".sidebar nav a:last-child { margin-top: auto; }", css)

    def test_logout_only_appears_in_settings(self) -> None:
        settings = (WEB / "settings.html").read_text(encoding="utf-8")
        self.assertIn('id="logout"', settings)
        self.assertIn("退出当前账号", settings)
        self.assertIn('id="logout-all"', settings)
        for filename in ("index.html", "errors.html", "reviews.html", "practice.html", "progress.html"):
            self.assertNotIn('id="logout"', (WEB / filename).read_text(encoding="utf-8"))

    def test_login_and_register_are_separate_documents(self) -> None:
        login = (WEB / "login.html").read_text(encoding="utf-8")
        register = (WEB / "register.html").read_text(encoding="utf-8")
        auth_script = (WEB / "auth.js").read_text(encoding="utf-8")
        self.assertIn('data-auth-mode="login"', login)
        self.assertIn('data-auth-mode="register"', register)
        self.assertNotIn('id="password"', login)
        self.assertIn('id="password" type="password"', register)
        for html in (login, register):
            self.assertIn('<form id="code-form">', html)
            self.assertIn('id="captcha-token"', html)
            self.assertIn('id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required disabled', html)
            self.assertIn('id="otp-button" disabled', html)
            self.assertIn('id="auth-submit" disabled', html)
            self.assertNotIn('href="#', html)
        self.assertIn('minlength="8" maxlength="20"', register)
        self.assertIn('/[A-Za-z]/.test(value)', auth_script)
        self.assertIn('/[0-9]/.test(value)', auth_script)
        self.assertIn("captcha_required", auth_script)
        self.assertIn("error.retryAfter", auth_script)
        self.assertIn("countdown > 0", auth_script)
        self.assertIn('$("#code").value = localTestCode;', auth_script)
        self.assertIn("仅限本地测试：模拟验证码已自动填入。", auth_script)
        self.assertIn('!validCode() || !$("#agreement").checked || authSubmitting', auth_script)
        self.assertNotIn('!passwordIsValid || !$("#agreement").checked', auth_script)
        self.assertIn('location.replace("/")', auth_script)

    def test_visual_tokens_remain_stable(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        for token in ("#002060", "#F6F5F1", "#182230", "#586474", "#D9DEE7"):
            self.assertIn(token, css)


if __name__ == "__main__":
    unittest.main()
