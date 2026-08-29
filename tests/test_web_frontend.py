import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class FrontendContractTests(unittest.TestCase):
    def test_product_pages_remain_independent_documents(self) -> None:
        pages = {
            "errors.html": ('data-page="errors"', 'id="all-errors"'),
            "practice.html": ('data-page="practice"', 'id="practice-errors"'),
            "progress.html": ('data-page="progress"', 'id="review-rule-heading"'),
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
            for route in ('href="/"', 'href="/errors"', 'href="/practice"', 'href="/progress"', 'href="/settings"'):
                self.assertIn(route, html)
            for icon in ("errors", "practice", "progress", "settings"):
                self.assertIn(f'/web/nav-icons.svg#{icon}', html)
            self.assertNotIn('href="/reviews"', html)
            self.assertNotIn('/web/nav-icons.svg#workbench', html)
            self.assertIn('aria-label="返回工作台"', html)
            self.assertIn('<span>设置</span>', html)
            self.assertNotIn('<span>设置与隐私</span>', html)
            self.assertNotIn('href="#', html)
            for other_marker in unique_markers:
                if other_marker != own_marker:
                    self.assertNotIn(other_marker, html)
        self.assertFalse((WEB / "reviews.html").exists())
        self.assertTrue((WEB / "progress.html").exists())

    def test_error_notebook_focuses_on_today_and_error_records(self) -> None:
        html = (WEB / "errors.html").read_text(encoding="utf-8")
        script = (WEB / "app.js").read_text(encoding="utf-8")
        for text in ("今日的复习计划", "全部错题"):
            self.assertIn(text, html)
        for text in ("六阶段复习规则", "主动提取", "间隔效应", "即时反馈", "各复习阶段"):
            self.assertNotIn(text, html)
        for marker in ("generate-review-pdf", "selected-error-count"):
            self.assertIn(f'id="{marker}"', html)
        self.assertNotIn('id="today-review-items"', html)
        self.assertNotIn("renderDueReviews", script)
        for contract in ('api("/v1/errors")', 'api("/v1/reviews/today")', 'api("/v1/progress")', 'api("/v1/practice-pdfs"'):
            self.assertIn(contract, script)
        self.assertIn('name="today-error"', script)
        self.assertIn('data-error-detail=', script)
        self.assertNotIn('id="error-detail"', html)
        self.assertNotIn("$$('", script)
        self.assertIn("today_needs_correction_count", script)

    def test_product_pages_show_deterministic_daily_learning_usage(self) -> None:
        script = (WEB / "app.js").read_text(encoding="utf-8")
        style = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn('api("/v1/learning-usage")', script)
        self.assertIn("今日学习负荷", script)
        self.assertIn("grade.count", script)
        self.assertIn("recommendation.count", script)
        self.assertIn(".learning-usage-strip", style)

    def test_learning_progress_owns_review_rules_and_activity_calendar(self) -> None:
        html = (WEB / "progress.html").read_text(encoding="utf-8")
        script = (WEB / "app.js").read_text(encoding="utf-8")
        for text in ("六阶段复习规则", "主动提取", "间隔效应", "即时反馈", "错题与复习日历", "新增错题", "应复习", "需改错", "逾期", "复习正确率"):
            self.assertIn(text, html)
        self.assertNotIn("各复习阶段", html)
        for marker in ("review-calendar", "calendar-month", "calendar-prev", "calendar-next", "calendar-summary", "calendar-stats", "calendar-filters", "calendar-day-detail", "calendar-day-items", "refresh-progress"):
            self.assertIn(f'id="{marker}"', html)
        self.assertNotIn('id="stage-count-1"', html)
        self.assertIn('function bindProgress()', script)
        self.assertIn('api(`/v1/progress/calendar?month=${monthKey()}`)', script)
        self.assertIn('data-calendar-filter', html)
        self.assertIn('data-calendar-date', script)
        self.assertIn('knowledge_points', script)

    def test_workbench_is_the_official_deepseek_harness_surface(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        plugin = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "client.js").read_text(encoding="utf-8")
        host_plugin = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").read_text(encoding="utf-8")
        patch = (ROOT / "config" / "deepseek-harness" / "web-product.patch.yml").read_text(encoding="utf-8")
        runtime_config = (ROOT / "config" / "deepseek-harness" / "cordis.yml").read_text(encoding="utf-8")
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
        for label, route in (("错题本", "/errors"), ("练习 PDF", "/practice"), ("学习进度", "/progress")):
            self.assertIn(f'path: "{route}", label: "{label}"', plugin)
        self.assertNotIn('path: "/reviews"', plugin)
        self.assertNotIn("今日复习", plugin)
        self.assertNotIn('path: "/", label: "工作台"', plugin)
        self.assertIn('ctx.slots.register({name: "conversation", priority: -1}, ProductSurface)', plugin)
        self.assertIn('data-lzlm-product-surface', plugin)
        self.assertIn('?embedded=1', plugin)
        self.assertIn('data-lzlm-product-path', plugin)
        self.assertIn("closeProductOnSessionClick(ctx)", plugin)
        self.assertIn("event.target.closest('[role=\"treeitem\"]')", plugin)
        self.assertNotIn('target: "_top",\n          title: item.label', plugin)
        self.assertNotIn('path: "/settings", label: "设置与隐私"', plugin)
        self.assertIn('id: "account-privacy"', plugin)
        self.assertIn('name: "settings.section"', plugin)
        self.assertIn('label: "账号与隐私"', plugin)
        self.assertIn('src: `${productOrigin}/settings?embedded=1`', plugin)
        self.assertIn('title: "账号与隐私"', plugin)
        self.assertIn('order: -10', plugin)
        self.assertIn('restrictStudentSettings(ctx)', plugin)
        self.assertIn('button.toggleAttribute("data-lzlm-student-hidden", button.textContent.trim() !== "账号与隐私")', plugin)
        self.assertIn('["打开配置文件", "Open configuration file"]', plugin)
        self.assertIn('[data-lzlm-student-hidden]', plugin)
        self.assertNotIn('href: `${productOrigin}/settings`', plugin)
        self.assertIn('aria-label": "错题本功能导航"', plugin)
        self.assertIn('button[aria-label="选择工作区"]', plugin)
        self.assertIn('button[aria-label="添加工作区"]', plugin)
        self.assertIn('button[aria-label="新建会话"]:has(svg)', plugin)
        self.assertIn('button[aria-label="New session"]:has(svg)', plugin)
        self.assertIn('item.title === productWorkspaceTitle', plugin)
        self.assertIn('ctx.workspaces.connectWorkspace(workspace.workspaceId)', plugin)
        self.assertIn('ctx.sessions.open(sessionId)', plugin)
        for plugin_id in ("ui-settings-models", "ui-settings-plugin-inventory", "ui-settings-plugins"):
            self.assertRegex(patch, rf"- id: {plugin_id}\s+disabled: true")
        self.assertIn('ctx.workspaceRegistry.create(workspacePath, "错题会话")', host_plugin)
        self.assertIn('LZLM_HARNESS_WORKSPACE_ROOT', host_plugin)
        self.assertIn('name: "confirm_error_notebook_entry"', host_plugin)
        self.assertIn('name: "process_error_notebook_attachments"', host_plugin)
        self.assertIn('name: "recheck_error_notebook_reference_conflict"', host_plugin)
        self.assertIn('name: "adjudicate_error_notebook_reference_conflicts"', host_plugin)
        self.assertIn('/v1/internal/harness/reference-conflicts/recheck', host_plugin)
        self.assertIn('/v1/internal/harness/reference-conflicts/adjudicate', host_plugin)
        self.assertNotIn('anyOf:', host_plugin)
        self.assertIn('ctx.attachments.readImage', host_plugin)
        self.assertIn('/v1/internal/harness/intakes/process', host_plugin)
        self.assertIn('latestUserImages(exec.agent)', host_plugin)
        self.assertIn('exec.concludeTurn()', host_plugin)
        self.assertIn('/v1/internal/harness/grade-results/', host_plugin)
        self.assertIn('/v1/harness/sessions/bind', plugin)
        self.assertIn('credentials: "include"', plugin)
        self.assertIn('dataset.lzlmSelectionActions', plugin)
        self.assertIn('textContent = "添加到对话"', plugin)
        self.assertIn('ancestor.closest("[data-chat-flow]")', plugin)
        self.assertIn('conversation.input.for(sessionContext)', plugin)
        self.assertIn('input.setDraft(', plugin)
        self.assertIn('installSelectionToConversation(ctx)', plugin)
        self.assertIn("ui-brand-official", patch)
        self.assertIn("ui-math-notebook", patch)
        self.assertIn("tool-bash", patch)
        self.assertIn("tool-pwsh", patch)
        self.assertIn("default: math-notebook", patch)
        self.assertIn("includeUserRoot: true", patch)
        self.assertIn("@deepseek-ai/dsh-persona", preset)
        self.assertNotIn("dsh-tool-", preset)
        self.assertIn("confirm_error_notebook_entry", preset)
        for prompt in (patch, preset, runtime_config):
            self.assertIn('process_error_notebook_attachments', prompt)
            self.assertIn('recheck_error_notebook_reference_conflict', prompt)
            self.assertIn('adjudicate_error_notebook_reference_conflicts', prompt)
            self.assertIn('receipt_message', prompt)
            self.assertNotIn('未收到判题流程返回', prompt)
            self.assertIn('最终答案', prompt)
            self.assertIn('*（小建议：……）*', prompt)
            self.assertIn('“## 下一步”', prompt)
            self.assertIn('只给出一个最优先', prompt)
            self.assertNotIn('最终答案及小建议', prompt)

    def test_harness_product_views_hide_the_legacy_sidebar(self) -> None:
        script = (WEB / "app.js").read_text(encoding="utf-8")
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn('new URLSearchParams(location.search).get("embedded") === "1"', script)
        self.assertIn("body.is-embedded .sidebar", css)
        self.assertIn("body.is-embedded main {", css)
        self.assertIn("margin-left: 0;", css)

    def test_harness_product_views_share_the_harness_visual_language(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("body.is-embedded {", css)
        self.assertIn("--ink: #0f1115;", css)
        self.assertIn("body.is-embedded .page-header", css)
        self.assertIn("body.is-embedded .panel {", css)
        self.assertIn("body.is-embedded button {", css)
        self.assertIn("body.is-embedded .stats div {", css)

    def test_brand_and_learning_contract_survive_harness_adoption(self) -> None:
        html = "".join(path.read_text(encoding="utf-8") for path in WEB.glob("*.html"))
        script = (WEB / "app.js").read_text(encoding="utf-8")
        for removed in ("昵称", "年级", "出生日期", "监护人", "家庭"):
            self.assertNotIn(removed, html + script)
        self.assertIn("next_action", (ROOT / "openapi" / "web-v1.json").read_text(encoding="utf-8"))
        self.assertIn("练习 PDF", html)
        self.assertIn("/v1/reviews/today", script)
        self.assertIn("/v1/practice-pdfs", script)
        self.assertIn('id="practice-pdf-history"', html)
        self.assertIn('item.source === "desktop_skill"', script)
        self.assertIn('Skill 历史文件', script)
        self.assertIn("已生成的 PDF", html)
        self.assertIn("/chat-turn", script)
        self.assertIn("await commitCurrent()", script)
        self.assertIn('localStorage.getItem("lzlm-device-id")', script)
        self.assertIn('"X-Device-ID": deviceId', script)
        for heading in ("题目整理", "学生作答还原", "错因分析与点评", "知识点梳理", "详细解析", "最终答案", "错题本记录检查"):
            self.assertIn(heading, script)
        self.assertIn('（小建议：${diagnosis.prevention_cue}）', script)
        self.assertNotIn('6. 最终答案及小建议', script)

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
        for filename in ("index.html", "errors.html", "practice.html", "progress.html"):
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
