# 独立判题字段契约修复

## 问题与范围

低 token 工具契约将 correct_solution、final_answer 声明为可选，但 independent 且非 unclear 时运行时要求两者非空。提示中的“只提交新生成的判定字段”未明确列出两个字段，导致模型遗漏后工具拒绝。校验原位于逐图写入循环中，后图缺字段还可能在前图已提交后才被发现。

## 验收约定

- 参数 schema 明确要求两个字段；independent 非 unclear 提交完整解法和各小题最终答案。
- verified_reference 使用空字符串，不回传参考长文；宿主仍注入当前授权参考。unclear 不编造解答。
- 在任何图片写入前检查所有未完成题目；错误指出题目位置和缺少的字段，并提供相同批次的补交方法。
- 保留冻结题干、批次所有权、参考版本校验与不重复写入行为。
- 运行合成 Harness 契约检查和项目全部 unittest，重启本地独立后台服务。

## 验证

- `python -X utf8 -B -m unittest discover -s tests -p test_harness_grading_contract.py` 通过。覆盖 correct/partial/incorrect 三种结果、两个解答字段的省略/空字符串/空白/null/非字符串、后图失败前零写入、同批次修复后每图恰好提交一次，以及 verified_reference 空字段补齐和 unclear 无解答。
- `python -X utf8 -B -m unittest discover -s tests -p "test_*.py"`：420 项全部通过，39.521 秒。
- `node --check extensions/dsh-math-notebook-ui/lib/index.js` 与 `git diff --check` 通过。
- 合成批次载荷仍由旧版 1445 UTF-8 字节减少为 597 字节；这是载荷字节数对照，不是实际模型 token 计费测量。
- 已通过项目的 PID 身份校验停止旧服务，并在隔离环境外以 `--daemon --enable-harness-model --enable-harness-ui` 启动独立后台服务。`/healthz` 与 `/` 均返回 200，MySQL 与本地配置检查正常。
- 本次未调用真实 Qwen API，也未重写截图对应的业务判题记录；历史失败消息仍需在 Web 页面重试，真实回执决定是否入库。
