import json
from pathlib import Path
import unittest


class WebOpenApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.document = json.loads((root / "openapi" / "web-v1.json").read_text(encoding="utf-8"))

    def operation(self, path: str, method: str) -> dict:
        return self.document["paths"][path][method]

    def test_otp_request_contract_has_actual_failure_statuses_and_retry(self) -> None:
        for path in ("/v1/auth/login/otp/request", "/v1/auth/register/otp/request"):
            responses = self.operation(path, "post")["responses"]
            self.assertTrue({"202", "400", "409", "428", "429", "503"}.issubset(responses))
            self.assertEqual(responses["429"]["$ref"], "#/components/responses/RetryLater")
        retry = self.document["components"]["responses"]["RetryLater"]
        self.assertTrue(retry["headers"]["Retry-After"]["required"])

    def test_cookie_write_contracts_require_forbidden_response(self) -> None:
        for path, method in (("/v1/auth/sensitive/otp/request", "post"), ("/v1/session", "delete"), ("/v1/sessions", "delete"), ("/v1/exports", "post"), ("/v1/account", "delete")):
            self.assertIn("403", self.operation(path, method)["responses"])

    def test_export_download_is_json_object_and_expiry_is_not_a_distinct_status(self) -> None:
        responses = self.operation("/v1/exports/{job_id}/download", "get")["responses"]
        self.assertEqual(responses["200"]["content"]["application/json"]["schema"]["type"], "object")
        self.assertIn("404", responses)
        self.assertNotIn("410", responses)
        self.assertEqual(set(self.document["components"]["schemas"]["ExportCreated"]["required"]), {"job_id", "download_url", "expires_at"})
        self.assertEqual(self.operation("/v1/exports", "post")["responses"]["201"]["$ref"], "#/components/responses/ExportCreated")

    def test_retryable_account_cleanup_failure_is_in_the_contract(self) -> None:
        self.assertIn("503", self.operation("/v1/account", "delete")["responses"])

    def test_every_domain_mutation_declares_validation_and_origin_failures(self) -> None:
        mutations = (
            ("/v1/files", "post"),
            ("/v1/intakes", "post"),
            ("/v1/intakes/{intake_id}", "patch"),
            ("/v1/intakes/{intake_id}/confirm", "post"),
            ("/v1/intakes/{intake_id}/manual-candidate", "post"),
            ("/v1/attempts/{attempt_id}/manual-grade", "post"),
            ("/v1/grade-results/{result_id}/commit", "post"),
            ("/v1/errors/{error_id}/recommendations", "post"),
            ("/v1/errors/{error_id}/master", "post"),
            ("/v1/errors/{error_id}", "delete"),
            ("/v1/reviews/{review_id}/complete", "post"),
            ("/v1/practice-pdfs", "post"),
            ("/v1/exports", "post"),
            ("/v1/account", "delete"),
        )
        for path, method in mutations:
            with self.subTest(path=path, method=method):
                self.assertTrue({"400", "403"}.issubset(self.operation(path, method)["responses"]))

    def test_auth_agreement_versions_match_the_server_constant(self) -> None:
        for schema_name in ("LoginVerify", "RegisterComplete"):
            properties = self.document["components"]["schemas"][schema_name]["properties"]
            self.assertEqual(properties["terms_version"]["const"], "2026-08-23")
            self.assertEqual(properties["privacy_version"]["const"], "2026-08-23")

    def test_upload_idempotency_conflict_is_in_the_contract(self) -> None:
        self.assertIn("409", self.operation("/v1/files", "post")["responses"])

    def test_full_diagnosis_and_local_bank_status_are_documented(self) -> None:
        manual = self.document["components"]["schemas"]["ManualGradeCandidate"]
        required = set(manual["allOf"][0]["then"]["required"])
        self.assertEqual(required, {"first_error", "cause_code", "evidence", "knowledge_points", "correct_solution", "final_answer"})
        self.assertIn("knowledge_points", self.document["components"]["schemas"]["Diagnosis"]["properties"])
        self.assertIn("200", self.operation("/v1/bank/status", "get")["responses"])

    def test_review_calendar_documents_month_and_activity_payload(self) -> None:
        operation = self.operation("/v1/progress/calendar", "get")
        self.assertEqual(operation["parameters"][0]["name"], "month")
        self.assertEqual(operation["responses"]["200"]["$ref"], "#/components/responses/ReviewCalendar")
        schema = self.document["components"]["schemas"]["ReviewCalendar"]
        self.assertEqual(set(schema["required"]), {"month", "total_error_count", "summary", "days"})

    def test_model_extraction_returns_all_questions_in_one_file(self) -> None:
        operation = self.operation("/v1/intakes/{intake_id}/model-candidate", "post")
        self.assertEqual(operation["responses"]["201"]["$ref"], "#/components/responses/IntakeBatch")
        self.assertEqual(operation["requestBody"]["content"]["application/json"]["schema"]["$ref"], "#/components/schemas/ModelCandidateRequest")
        batch = self.document["components"]["schemas"]["IntakeBatch"]
        self.assertEqual(batch["allOf"][1]["properties"]["items"]["maxItems"], 20)
        self.assertIn("item_no", self.document["components"]["schemas"]["Intake"]["required"])

    def test_model_transport_failures_have_stable_error_codes(self) -> None:
        codes = set(self.document["components"]["schemas"]["ErrorEnvelope"]["properties"]["error"]["properties"]["code"]["enum"])
        self.assertTrue({"model_network_error", "model_rate_limited", "model_authentication_error"} <= codes)

    def test_conversation_history_hides_internal_thread_and_prompt_fields(self) -> None:
        operation = self.operation("/v1/conversations/latest/messages", "get")
        self.assertEqual(operation["responses"]["200"]["$ref"], "#/components/responses/ConversationHistory")
        schema = self.document["components"]["schemas"]["ConversationHistory"]
        self.assertEqual(set(schema["properties"]), {"items", "next_cursor"})
        self.assertEqual(schema["properties"]["items"]["maxItems"], 20)
        self.assertEqual(operation["parameters"][0]["name"], "cursor")
        message = self.document["components"]["schemas"]["ConversationMessage"]
        self.assertEqual(set(message["properties"]), {"role", "text", "attachments"})
        self.assertEqual(message["properties"]["attachments"]["items"]["$ref"], "#/components/schemas/Attachment")

    def test_owned_intake_source_image_is_documented(self) -> None:
        operation = self.operation("/v1/intakes/{intake_id}/source", "get")
        self.assertEqual(operation["operationId"], "getIntakeSourceImage")
        self.assertTrue({"401", "404"}.issubset(operation["responses"]))
        self.assertEqual(set(operation["responses"]["200"]["content"]), {"image/png", "image/jpeg"})

    def test_conversation_control_uses_owned_intake_routes(self) -> None:
        stop = self.operation("/v1/intakes/{intake_id}/conversation/stop", "post")
        compact = self.operation("/v1/intakes/{intake_id}/conversation/compact", "post")
        self.assertEqual(stop["operationId"], "stopMathNotebookConversationTurn")
        self.assertEqual(compact["operationId"], "compactMathNotebookConversation")
        self.assertTrue({"404", "409", "503"}.issubset(compact["responses"]))

    def test_clear_conversation_preserves_notebook_scope(self) -> None:
        operation = self.operation("/v1/conversations/latest", "delete")
        self.assertEqual(operation["operationId"], "clearLatestConversation")
        self.assertIn("不删除已入本错题", operation["description"])
        self.assertTrue({"204", "401", "403"}.issubset(operation["responses"]))


if __name__ == "__main__":
    unittest.main()
