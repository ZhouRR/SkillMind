"""YAML source の検査は PyYAML 依存とし、標準 library の wrapper 回帰から分離する。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml

from scripts import validate_compose


class ComposeSourceInvariantTests(unittest.TestCase):
    """YAML 静的検証でも三つの env_file と image pin 入口の退行を拒否する。"""

    def test_repository_compose_invariants_pass_without_dotenv_or_docker(self) -> None:
        """Compose source だけを読み、実環境へ接続しない。"""

        with redirect_stdout(io.StringIO()):
            validate_compose.validate()

    def test_each_backend_service_must_use_same_explicit_source(self) -> None:
        """一 service だけ .env へ戻っても validator が失敗する。"""

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

    def test_each_application_image_must_support_explicit_pins(self) -> None:
        """mutable tag 直書きが一箇所へ戻った場合も検出する。"""

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
