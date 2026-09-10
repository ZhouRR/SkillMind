"""契約 validator の登録漏れと原 upload の状態整合を回帰する。"""

from __future__ import annotations

import json
import unittest

from jsonschema import FormatChecker
from jsonschema.validators import validator_for

from scripts import validate_contracts


class UploadExampleRegistrationTests(unittest.TestCase):
    """CLI と Backend の共通 example を確実に CLI 検証対象へ含める。"""

    def test_original_upload_examples_are_registered_and_valid(self) -> None:
        """未確定と公開済みの両方を保存 Schema で検証する。"""

        for state in ("pending", "published"):
            with self.subTest(state=state):
                name = f"examples/document-upload-{state}.v1.json"
                schema_path = validate_contracts.EXAMPLE_CONTRACTS[name]
                self.assertEqual(schema_path, "documents/v1/upload.schema.json")
                schema = validate_contracts.load_json(validate_contracts.CONTRACTS / schema_path)
                validator = validator_for(schema)(schema, format_checker=FormatChecker())
                example = validate_contracts.load_json(validate_contracts.CONTRACTS / name)
                validator.validate(example)
                example["created_at"] = "2026-09-10T12:00:00"
                self.assertFalse(validator.is_valid(example))
                example = validate_contracts.load_json(validate_contracts.CONTRACTS / name)
                if state == "published":
                    example["document"]["created_at"] = "2026-09-10T12:00:00"
                    self.assertFalse(validator.is_valid(example))
                    example = validate_contracts.load_json(validate_contracts.CONTRACTS / name)
                example["state"] = "PUBLISHED" if state == "pending" else "PENDING"
                self.assertFalse(validator.is_valid(example))

    def test_independent_closure_examples_are_registered_without_extending_upload(self) -> None:
        """原 upload の二状態を維持し、明示要求と閉鎖回执を別契約として検証する。"""

        for name in ("upload-closure", "upload-closure-request"):
            relative = f"examples/document-{name}.v1.json"
            self.assertEqual(
                validate_contracts.EXAMPLE_CONTRACTS[relative], f"documents/v1/{name}.schema.json",
            )
            schema = validate_contracts.load_json(
                validate_contracts.CONTRACTS / validate_contracts.EXAMPLE_CONTRACTS[relative],
            )
            example = validate_contracts.load_json(validate_contracts.CONTRACTS / relative)
            validator_for(schema)(schema, format_checker=FormatChecker()).validate(example)
        original = validate_contracts.load_json(
            validate_contracts.CONTRACTS / "documents/v1/upload.schema.json",
        )
        self.assertEqual(original["properties"]["state"]["enum"], ["PENDING", "PUBLISHED"])


class ArtifactExampleRegistrationTests(unittest.TestCase):
    """新しい array 応答を Schema object の読取と混同せず検証対象に含める。"""

    def test_array_artifact_example_and_both_tool_receipts_are_registered(self) -> None:
        """不変 output と可変 workspace の両経路に必須 example を持たせる。"""

        for name in (
            "artifact-list.v1.json", "workspace-write-response.v2.json",
            "workspace-write-draft-response.v2.json", "run-detail-artifacts.v1.json",
        ):
            relative = f"examples/{name}"
            schema = validate_contracts.load_json(
                validate_contracts.CONTRACTS / validate_contracts.EXAMPLE_CONTRACTS[relative],
            )
            example = json.loads(
                (validate_contracts.CONTRACTS / relative).read_text(encoding="utf-8"),
            )
            validator_for(schema)(schema, format_checker=FormatChecker()).validate(example)
        with self.assertRaises(TypeError):
            validate_contracts.load_json(
                validate_contracts.CONTRACTS / "examples/artifact-list.v1.json",
            )


class EvaluationExampleRegistrationTests(unittest.TestCase):
    """原要求の回执と履歴ページを旧追加式応答から独立して検証する。"""

    def test_submission_and_page_examples_are_registered_and_valid(self) -> None:
        """内部 hash/原 session を含めず、nullable cursor と正確な保存値を公開する。"""

        for name in (
            "evaluation-submission-request.v1.json", "evaluation-submission.v1.json",
            "evaluation-page.v1.json",
        ):
            relative = f"examples/{name}"
            schema = validate_contracts.load_json(
                validate_contracts.CONTRACTS / validate_contracts.EXAMPLE_CONTRACTS[relative],
            )
            example = validate_contracts.load_json(validate_contracts.CONTRACTS / relative)
            validator_for(schema)(schema, format_checker=FormatChecker()).validate(example)


if __name__ == "__main__":
    unittest.main()
