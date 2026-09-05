# ProjectMind Backend

FastAPI API と ARQ Worker が同じ `projectmind` package を共有するモジュラーモノリス。API は認証/入出力、service は業務調整、repository は transaction、AgentEngine/Provider は実行境界を担当する。

[システム構成](../../docs/overview/architecture.md) · [ローカル開発](../../docs/development/local-development.md) · [変更規約](../AGENTS.md)

## 実装の入口

| Directory | 責務 |
| --- | --- |
| [api](src/projectmind/api/) | actor dependency、資源別 route、Problem |
| [skills](src/projectmind/skills/) / [compositions](src/projectmind/compositions/) | 導入・解釈・公開・組織資産・Project 有効化 |
| [runs](src/projectmind/runs/) / [worker](src/projectmind/worker/) | Segment/Attempt、Outbox、lease、待機・復旧・終態 |
| [agent](src/projectmind/agent/) | SDK adapter、Tool Gateway、workspace、物化、Evidence、子 Agent |
| [integrations](src/projectmind/integrations/) / [documents](src/projectmind/documents/) | 資源・Secret・binding・Project 文書 |
| [effects](src/projectmind/effects/) / [schedules](src/projectmind/schedules/) | 受控 write・時刻起動 |
| [auth](src/projectmind/auth/) / [projects](src/projectmind/projects/) | Session、ユーザー、Project と成员 |
| [db](src/projectmind/db/) / [migrations](migrations/) | 実 table・migration |
| [tests](tests/) | src の module 構成に対応する回帰 |

`modules/` は生成 FrontendModule の前置検査のみ。構築/配信 service は未実装。Blueprint は JSON 内嵌、ExecutableTask/RunStep は投影概念であり、同名 table の存在を仮定しない。

## 起動と検証

環境構築・DB 接続設定は[ローカル開発](../../docs/development/local-development.md)を参照する。依存導入後、`backend/` で API を起動する。

```bash
python3 -m uvicorn projectmind.api.main:app --reload --port 8000
```

同じ設定で Worker を別 process として起動する場合：

```bash
arq projectmind.worker.settings.WorkerSettings
```

Worker dispatch は明示有効化が必要。DB/Redis/object storage が別途必要であり、production 相当の構成は[Compose 起動](../../docs/operations/quickstart.md)に従う。

Backend の変更は Ruff/Mypy/Pytest と関連する契約/SDK probe を確認する。実 DB test の skip は未検証として報告する。
