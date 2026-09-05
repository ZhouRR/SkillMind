---
name: pjm-project-dev
description: ProjectMind(PJM) 项目开发约束与工作流。Use when working in the ProjectMind repository (/app/pjm, ProjectMind, PJM, projectmind) for code changes, refactors, reviews, tests, or verification — enforces AGENTS.md conventions, Japanese comments, contract sync points, and the no-venv verify-then-clean workflow.
---

# ProjectMind (PJM) Project Dev

在 ProjectMind 仓库工作时遵守以下约束。

## 必读与事实来源

- 动手前先读 `AGENTS.md`（拘束规约全文）；本 Skill 只是操作层补充，冲突时以 `AGENTS.md` 为准。
- 正式规格在 `docs/`，可执行契约在 `contracts/`。规格与代码矛盾时不要擅自改任何一边，先说明影响面再对齐。
- 工作区不是 git 仓库：禁止 git init/commit/push；不要用 git diff 判断改动，用 `rg` + 行号与片段说明。

## 语言与注释

- 对话与交付报告用中文；代码 docstring/JSDoc/行内注释、README/AGENTS/子目录 README 用日文。
- 标识符、公开 API field、Schema key、协议名、外部产品名保持英文。
- 注释写「为什么需要/守护什么」，不逐句翻译代码；修改逻辑时同步更新旧注释。

## 结构约定（新代码放哪）

- 后端公开 endpoint：`backend/src/projectmind/api/routes/` 资源别 module **单层**实现；认证边界用 `api/auth_dependencies.py` 的 actor alias（`ReadActor` / `WriteActor` / `ProjectReadActor` / `ProjectWriteActor` / `AdminReadActor` / `AdminWriteActor`）声明；不要再引入未认证的并行实现或委托 wrapper。
- 错误一律 `ProblemException`，并复用共享工厂：`auth_dependencies` 的 401/403/404 工厂、`routes/runs.py` 的 `run_not_found_problem` / `authorized_run`；不要在 route 里复制同义 Problem。
- 共通实现走单点（AGENTS「共通実装の利用規約」）：`core/hashing.py` 的 canonical_json/sha256_hex、`core/redaction.py` 的 find_sensitive_key、`runs/domain.py` 的 lease_token_hash、`RunRepository` 的 event/outbox 方法。
- Web：API client 在 `web/src/api/` 资源别 module，公开面只经 `index.ts` barrel；HTTP 一律走 `api/http.ts` 的 `requestApiJson` / `requestApiEmpty`（类型化 `ApiProblemError`），不直接调 fetch；mutation 函数带 `csrfToken` 参数并发送 `X-CSRF-Token`；画面无关纯逻辑放 `src/lib/`。
- 测试镜像 `src` 模块结构放在 `tests/<module>/`；API contract 测试按资源拆在 `tests/api/`，共享 fake 在 `tests/api/fakes.py`，默认认证 client fixture 在 `tests/api/conftest.py`。

## 同步点（改一处必须跟着改的地方）

- 设置项：`core/settings.py` → `.env.example` → `api/main.py` lifespan 或 `worker/settings.py` startup。
- AgentEventType：`agent/domain.py` → `agent/engine.py` → `web/src/api/events.ts` 的 `RUN_EVENT_NAMES` → `contracts/events/run-event/v1.schema.json`。
- 公开 endpoint：`routes/` 资源 module（actor alias + response model 显式列举）→ `web/src/api/` 资源 module + `index.ts` 再导出 → 契约 schema/example → 前后端测试。
- DB model：`db/models.py` → Alembic migration → repository DTO → 测试。
- `contracts/openapi/projectmind-api.v1.json` 现为 LF，且与 `python3 scripts/export_openapi.py` 输出逐字节一致（2026-07-09 起，早期"CRLF 勿重导出"的说明已不成立）。新增/修改公开 endpoint 后用该脚本回写即可；契约一致性另有 pytest 的 dict 相等断言（`tests/contracts/`）守护。

## 验证与收尾（本机 WSL 容器工作流）

- 不用虚拟环境：依赖装用户站点 `cd backend && python3 -m pip install --user -e ".[dev]"`（缺依赖时执行一次）。
- 后端验证：`python3 -m ruff check .`、`python3 -m mypy src`、`python3 -m pytest`。
- Web 验证：`web/node_modules/.bin/tsc -b --pretty false`、`.bin/vitest run`、`.bin/vite build`；如需重装依赖必须 `CI=true npx -y pnpm@11.7.0 install --frozen-lockfile`（无 TTY 时缺 CI=true 会假成功）。
- 仓库级：`python3 scripts/validate_contracts.py`、`python3 scripts/validate_compose.py`、`PYTHONPATH=backend/src python3 scripts/probe_claude_agent_sdk.py`。本机无 docker，`make config` / `make smoke` 跑不了；未执行的验证要在报告中说明理由与残余风险（AGENTS 完了条件）。
- 收尾清理临时物：`backend/{.venv,.mypy_cache,.pytest_cache,.ruff_cache}`、仓库内 `__pycache__`（node_modules 除外）、`web/dist`、`web/node_modules/.tmp`、`**/*.egg-info`。node_modules 本体保留。
- Windows 同步来的文件可能是 root:644（当前用户不可写）：目录是 777，用「读内容 → os.unlink → 重写」的方式替换。

## 输出习惯

- 修改说明用中文，先给结论再给依据；引用代码用 `路径:行号`。
- 因遵守约束（最小影响、契约冻结等）做出的保守取舍，直接说明理由。
- Secret、内部 URL、票据正文不写入代码、fixture、日志或本 Skill 文档。
