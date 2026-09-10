"""Compose source の共有 image/config と ingress 境界を検査する。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml

from scripts import validate_compose


class ComposeSourceInvariantTests(unittest.TestCase):
    """Backend の設定源・export tag の不一致を拒否する。"""

    def test_repository_compose_invariants_pass_without_dotenv_or_docker(self) -> None:
        """Compose source だけを読み、実環境へ接続しない。"""

        with redirect_stdout(io.StringIO()):
            validate_compose.validate()

    def test_each_backend_service_must_use_same_explicit_source(self) -> None:
        """ENV_FILE の選択が一 service にだけ伝わらない変更を拒否する。"""

        document = yaml.safe_load(validate_compose.COMPOSE_FILE.read_text(encoding="utf-8"))
        for name in ("api", "worker", "migrate"):
            with self.subTest(name=name):
                changed = json.loads(json.dumps(document))
                changed["services"][name]["env_file"] = [".env"]
                with (
                    patch.object(validate_compose.yaml, "safe_load", return_value=changed),
                    self.assertRaisesRegex(ValueError, "selected environment file"),
                ):
                    validate_compose.validate()

    def test_web_build_context_cannot_drop_sibling_contracts(self) -> None:
        """local だけで通る test import を Docker build でも解決できる構造を守る。"""

        document = yaml.safe_load(validate_compose.COMPOSE_FILE.read_text(encoding="utf-8"))
        for key, value in (("context", "web"), ("dockerfile", "Dockerfile")):
            with self.subTest(key=key):
                changed = json.loads(json.dumps(document))
                changed["services"]["web"]["build"][key] = value
                with (
                    patch.object(validate_compose.yaml, "safe_load", return_value=changed),
                    self.assertRaisesRegex(ValueError, "sibling contracts"),
                ):
                    validate_compose.validate()

    def test_backend_pull_policy_prevents_implicit_downloads(self) -> None:
        """run --pull に頼らず、共通設定で不足 image の自動取得を拒否する。"""

        document = yaml.safe_load(validate_compose.COMPOSE_FILE.read_text(encoding="utf-8"))
        for name in ("api", "worker", "migrate"):
            with self.subTest(name=name):
                changed = json.loads(json.dumps(document))
                changed["services"][name].pop("pull_policy")
                with (
                    patch.object(validate_compose.yaml, "safe_load", return_value=changed),
                    self.assertRaisesRegex(ValueError, "pull_policy"),
                ):
                    validate_compose.validate()

    def test_each_application_image_must_match_export_tags(self) -> None:
        """一 service だけ別の image tag を使う変更を拒否する。"""

        document = yaml.safe_load(validate_compose.COMPOSE_FILE.read_text(encoding="utf-8"))
        for name in ("api", "worker", "migrate", "web"):
            with self.subTest(name=name):
                changed = json.loads(json.dumps(document))
                changed["services"][name]["image"] = "old:latest"
                with (
                    patch.object(validate_compose.yaml, "safe_load", return_value=changed),
                    self.assertRaises(ValueError),
                ):
                    validate_compose.validate()


if __name__ == "__main__":
    unittest.main()
