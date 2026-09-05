# 実行可能なインターフェース契約

本 directory は ProjectMind の公開契約を JSON Schema と代表 example で管理する。仕様の本文は
[文書ガイド](../../docs/README.md)から辿る `../../docs/design/` に置き、ここには機械検証可能な Schema、example、OpenAPI snapshot だけを置く。

## 配置

- 資源別 directory(`runs/`、`skills/`、`projects/`、`events/`、`errors/` など):公開 API・event・error の versioned Schema。
- `tools/<capability>/`:`issue.read/v1` のような versioned Tool capability の request/response/error Schema。
- `agent-task-brief/`、`capability-blueprint/`、`runtime-manifest/`、`outcomes/`、`view-spec/`:Interpreter と Runtime が共有する meta 契約。業務固有 field を追加しない。
- `examples/`:各 Schema の代表 example。
- `fixtures/`:offline 回帰用の固定入力。
- `openapi/projectmind-api.v1.json`:`python3 scripts/export_openapi.py` が現在の FastAPI application から再生成する snapshot。手編集しない。

## 同期義務

- Schema・example の追加/変更時は `scripts/validate_contracts.py` と
  `backend/tests/contracts/test_contracts.py` の**両方**の example 表へ登録する。片方だけでは
  example は誰にも検証されない。
- 公開 endpoint の追加/変更後は `scripts/export_openapi.py` で OpenAPI snapshot を回写する。
  一致性は `backend/tests/contracts/` の dict 相等 assertion が守る。
- Backend DTO、Web validator、テストとの同期点の全体は [AGENTS.md](../AGENTS.md) の
  「よくある変更の同期点」を参照する。

## 検証

以下は `contracts/` 内ではなく `PJM/` で実行する。公開 endpoint を変更した場合のみ OpenAPI を再生成する。Schema の存在は Provider/renderer 実装済みの証明にはならない。

```bash
python3 scripts/validate_contracts.py
```
