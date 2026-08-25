from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class FrontendContractTests(unittest.TestCase):
    def test_sidebar_routes_are_independent_documents(self) -> None:
        pages = {
            "index.html": ('data-page="workbench"', 'id="upload-form"'),
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

    def test_brand_and_learning_contract_survive_page_split(self) -> None:
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
        self.assertIn("确认写入错题本", script)
        self.assertIn('localStorage.getItem("lzlm-device-id")', script)
        self.assertIn('"X-Device-ID": deviceId', script)

    def test_mobile_layout_and_keyboard_focus_are_defined(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:720px)", css)
        self.assertIn(":focus-visible", css)
        self.assertNotIn("min-width:720px", css)
        self.assertIn(".sidebar nav a:last-child { margin-top: auto; }", css)

    def test_workbench_upload_supports_multiple_files_drag_and_paste(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        script = (WEB / "app.js").read_text(encoding="utf-8")
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn('type="file" accept=".pdf,.png,.jpg,.jpeg,.docx" multiple', html)
        self.assertIn('id="upload-file-list"', html)
        self.assertLess(html.index('id="upload-file-list"'), html.index('</div><input id="file"'))
        self.assertIn('dropZone.addEventListener("click"', script)
        self.assertIn('const uploadSurface = $(".chat-main")', script)
        self.assertIn('uploadSurface.addEventListener("dragover"', script)
        self.assertIn('uploadSurface.addEventListener("drop"', script)
        self.assertIn('uploadSurface.contains(event.relatedTarget)', script)
        self.assertIn('document.addEventListener("paste"', script)
        self.assertIn("item.file.lastModified === file.lastModified", script)
        self.assertIn("已忽略 ${duplicates} 个重复文件", script)
        self.assertIn("for (const file of files)", script)
        self.assertIn("URL.createObjectURL(file)", script)
        self.assertIn('id="image-preview-dialog"', html)
        self.assertIn("imagePreviewButton(item)", script)
        self.assertIn("previewDialog.showModal()", script)
        self.assertIn('[data-preview-url]', script)
        self.assertIn(".image-preview-dialog::backdrop", css)
        self.assertIn(".image-preview-dialog[open] { display: grid; place-items: center; }", css)
        self.assertIn(".image-preview-dialog > img { display: block; width: auto;", css)
        self.assertIn("new XMLHttpRequest()", script)
        self.assertIn('xhr.upload.addEventListener("progress"', script)
        for state in ('"queued"', '"uploading"', '"processing"', '"done"', '"failed"'):
            self.assertIn(state, script)
        self.assertIn(".chat-main.drag-active::after", css)
        self.assertIn('content: "松开即可添加文件"', css)
        self.assertIn(".upload-thumbnail", css)
        self.assertIn(".upload-progress", css)

    def test_workbench_is_one_chat_flow_with_one_composer(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        script = (WEB / "app.js").read_text(encoding="utf-8")
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn('class="chat-workspace"', html)
        self.assertIn('id="chat-thread" class="chat-thread"', html)
        self.assertIn('id="chat-stream" class="chat-stream"', html)
        self.assertIn('id="upload-form" class="chat-composer"', html)
        self.assertIn('id="chat-input" rows="1"', html)
        self.assertIn('id="composer-actions" class="composer-actions" role="radiogroup"', html)
        self.assertIn('type="radio" name="composer-action" value="ask" checked', html)
        self.assertIn("添加未判或已判的题目，我会陪您整理错题、分析错误原因、梳理相关知识点，并完成题目解析。", html)
        chat_input = html.split('id="chat-input"', 1)[1].split("</textarea>", 1)[0]
        self.assertNotIn("disabled", chat_input)
        self.assertNotIn('id="manual-flow"', html)
        self.assertNotIn('id="manual-intake-form"', html)
        self.assertNotIn('id="manual-grade-form"', html)
        self.assertIn('class="composer-surface"', html)
        self.assertNotIn('id="error-count"', html)
        self.assertNotIn('id="error-list"', html)
        self.assertNotIn('api("/v1/workbench")', script)
        self.assertIn(".chat-main", css)
        self.assertIn(".chat-composer", css)
        self.assertIn(".chat-turn", css)
        self.assertIn(".chat-upload-bubble", css)
        self.assertIn(".chat-progress-steps li.is-active", css)
        self.assertIn("appendUserUpload(files)", script)
        self.assertIn("progressTurn()", script)
        self.assertIn('className = "chat-progress-steps"', script)
        self.assertIn("!item.submitted", script)
        self.assertIn("item.submitted = true", script)
        self.assertIn("item.submitted = false", script)
        self.assertIn('recognition_failed: "识别失败"', script)
        self.assertIn('"题目识别未完成"', script)
        self.assertIn("文件已经保存，可直接重试识别，不会重复上传。", script)
        self.assertIn('"正在上传附件"', script)
        self.assertIn('"正在建立错题会话"', script)
        self.assertIn("正在识别题目与作答", script)
        self.assertIn("candidate.items", script)
        self.assertIn("JSON.stringify({refresh: true})", script)
        self.assertIn("item.intakeId = task.resource_id", script)
        self.assertIn("model_network_error", script)
        self.assertIn("题干与作答候选 · 进度 ${intake.queueIndex || 1}/${intake.queueTotal || 1}", script)
        self.assertIn("后面还有 ${pendingIntakes.length} 道题", script)
        self.assertIn('if (activeCandidate.verdict === "correct")', script)
        self.assertIn('activateNextIntake("本题判定正确，无需写入错题本。" )', script)
        self.assertIn('const result = await api("/v1/intakes")', script)
        self.assertIn('const result = await api("/v1/conversations/latest/messages")', script)
        self.assertIn("await restoreConversationHistory();", script)
        self.assertIn("await restorePendingIntakes();", script)
        self.assertLess(script.index("await restoreConversationHistory();"), script.index("await restorePendingIntakes();"))
        self.assertIn('activateNextIntake("已恢复上次尚未处理完的题目。" )', script)
        self.assertIn("已从 ${completed} 个文件识别 ${recognized} 道题", script)
        self.assertNotIn("Codex", html + script)
        self.assertIn("/model-candidate", script)
        self.assertIn("/model-grade", script)
        self.assertIn("/chat-turn", script)

    def test_workbench_uses_harness_style_history_window_and_docked_interactions(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        script = (WEB / "app.js").read_text(encoding="utf-8")
        css = (WEB / "app.css").read_text(encoding="utf-8")
        stream = html.split('id="chat-stream"', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("textarea", stream)
        self.assertNotIn("button", stream)
        self.assertGreater(html.index('id="upload-form"'), html.index('id="chat-stream"'))
        self.assertIn('id="load-older"', html)
        self.assertIn("let historyCursor = null", script)
        self.assertIn("/v1/conversations/latest/messages?cursor=", script)
        self.assertIn("appendHistoryItems(result.items, true)", script)
        self.assertIn("thread.scrollHeight - height", script)
        self.assertIn('id="compact-conversation"', html)
        self.assertIn("/conversation/compact", script)
        self.assertIn("/conversation/stop", script)
        self.assertIn('sendButton.textContent = stopRequested ? "…" : "■"', script)
        self.assertIn('document.createElement("details")', script)
        self.assertIn("progress.disclosure.open = state === \"error\"", script)
        self.assertIn("setComposerState()", script)
        self.assertIn("chatInput.disabled = false", script)
        self.assertIn('[["ask", "询问或修正"], ["confirm-intake", "确认并判题"], ["next", "下一题"]]', script)
        self.assertIn('[["commit", "确认入本"]]', script)
        self.assertIn('const fixedMessages = {"confirm-intake": "确认并判题", commit: "确认入本", next: "下一题"}', script)
        self.assertIn('function selectedComposerAction()', script)
        self.assertIn('if (chatInput.value.trim() && selectedComposerAction() !== "ask") selectComposerAction("ask")', script)
        self.assertIn("event.isComposing || event.keyCode === 229", script)
        self.assertIn("请先添加题目图片、PDF 或 DOCX", script)
        self.assertIn("userTurn(message)", script)
        self.assertIn("appendCandidate(activeCandidate)", script)
        self.assertIn("window.renderMathInElement", script)
        self.assertIn('output: "mathml"', script)
        self.assertIn('throwOnError: false', script)
        self.assertIn('replace(/^(\\s*[A-D][.、．]\\s*)\\\\+\\s*$/gm, "$1")', script)
        self.assertIn("renderMath(question)", script)
        self.assertIn('renderMath($("#error-detail"))', script)
        self.assertIn('new Set(["确认并判题", "确认题干与作答", "开始判题"])', script)
        self.assertIn('new Set(["确认入本", "确认写入错题本", "加入错题本"])', script)
        self.assertIn(".history-pagination", css)
        self.assertIn("#chat-input", css)
        self.assertIn(".composer-actions", css)
        self.assertIn(".composer-action input:checked + span", css)
        self.assertIn(".chat-disclosure-summary", css)
        self.assertIn(".math-content math", css)

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
        self.assertNotIn("routeHash", auth_script)
        self.assertNotIn("popstate", auth_script)

    def test_visual_tokens_remain_stable(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        for token in ("#002060", "#F6F5F1", "#182230", "#586474", "#D9DEE7"):
            self.assertIn(token, css)


if __name__ == "__main__":
    unittest.main()
