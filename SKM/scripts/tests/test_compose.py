"""Docker 不使用で application image の同梱範囲を検査する。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class BackendPackagingSourceTests(unittest.TestCase):
    """配備 image の Skill 同梱範囲を Docker 不使用で検査する。"""

    def test_native_build_defaults_to_dotenv_without_runtime_build_args(self) -> None:
        """直接 build の既定 file と、秘密を build args に渡さない境界を固定する。"""

        source = (ROOT / "compose.yml").read_text(encoding="utf-8")
        services = yaml.safe_load(source)["services"]
        for role in ("api", "worker", "migrate"):
            self.assertEqual(services[role]["env_file"], ["${ENV_FILE:-.env}"])
        self.assertNotIn("args", services["api"]["build"])
        self.assertEqual(
            services["web"]["build"]["args"],
            {"SKILLMIND_CONTEXT_PATH": "${SKILLMIND_CONTEXT_PATH:-/skillmind}"},
        )

    def test_backend_image_copies_only_the_system_interpreter_skill(self) -> None:
        """Skill 全体や example の再同梱を検知し、実行資産の存在も確かめる。"""

        root = ROOT
        source = (root / "backend/Dockerfile").read_text(encoding="utf-8")
        copies = [
            line.split()
            for line in source.splitlines()
            if line.startswith(("COPY ", "ADD ")) and "--from=" not in line
        ]
        skill_sources = [
            item
            for instruction in copies
            for item in instruction[1:-1]
            if item == "." or item.startswith("skills")
        ]
        self.assertEqual(skill_sources, ["skills/skillmind-skill-interpreter"])
        self.assertTrue((root / skill_sources[0] / "SKILL.md").is_file())
        self.assertIn(
            "COPY skills/skillmind-skill-interpreter /app/skills/skillmind-skill-interpreter",
            source,
        )

    def test_development_fixtures_are_excluded_from_the_backend_build_context(self) -> None:
        """合成 fixture や再追加された example を image builder へ送らない。"""

        patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("backend/tests", patterns)
        self.assertIn("skills/examples", patterns)


class WebPackagingSourceTests(unittest.TestCase):
    """型検査用の契約と、本番静的 image の同梱範囲を分けて検証する。"""

    def test_builder_preserves_contract_imports_but_runtime_copies_only_dist(self) -> None:
        """test を除外せず、web/ と contracts/ の同階層を builder に再現する。"""

        root = ROOT
        source = (root / "web/Dockerfile").read_text(encoding="utf-8")
        builder, runtime = source.split("FROM nginx:", 1)
        for instruction in (
            "WORKDIR /build/web",
            "COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./",
            "COPY web/ ./",
            "COPY contracts/ /build/contracts/",
            "RUN pnpm build",
        ):
            self.assertIn(instruction, builder)
        copies = [line for line in runtime.splitlines() if line.startswith(("COPY ", "ADD "))]
        self.assertEqual(
            copies,
            [
                "COPY web/nginx.conf /etc/nginx/nginx.conf",
                "COPY --from=builder /build/web/dist /usr/share/nginx/html",
            ],
        )
        config = json.loads((root / "web/tsconfig.app.json").read_text(encoding="utf-8"))
        self.assertIn("tests", config["include"])

    def test_web_context_allowlist_excludes_local_environment_and_dependencies(self) -> None:
        """Dockerfile 固有 ignore を使い、root の Backend 除外と衝突させない。"""

        source = (ROOT / "web/Dockerfile.dockerignore").read_text(encoding="utf-8")
        patterns = [line for line in source.splitlines() if line and not line.startswith("#")]
        self.assertEqual(patterns[:5], ["**", "!web/", "!web/**", "!contracts/", "!contracts/**"])
        self.assertFalse(any(line.startswith("!") for line in patterns[5:]))
        for excluded in ("**/.env", "**/.env.*", "**/node_modules", "**/dist"):
            self.assertIn(excluded, patterns[5:])


if __name__ == "__main__":
    unittest.main()
