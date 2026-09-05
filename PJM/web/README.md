# ProjectMind Web

React + TypeScript の application shell。ページの責務と現在の hash route は [Workspace 設計](../../docs/design/workspace.md)、次期の流れ表示は [Task Flow 設計](../../docs/design/task-flow.md)を参照する。

## 構成と境界

| Directory | 責務 |
| --- | --- |
| [src/pages](src/pages/) | 概览、Skills、Project、Workspace、History、Task Center、Documents、Resources |
| [src/components](src/components/) | 入力・実行・結果・待機・承認の共有 UI |
| [src/api](src/api/) | 資源別 client と response validator；画面は index.ts barrel から import |
| [src/lib](src/lib/) | routing、SSE projection、task draft など画面非依存 logic |
| [src/lib/i18n](src/lib/i18n/) | zh/ja/en catalog；React 側は useMessages 経由 |
| [src/styles](src/styles/) / [src/assets](src/assets/) | theme・responsive layout・自己保持 font |

Task Center は選択と調度、Workspace は一つの Run の lifecycle を担当する。共有入力は TaskLaunchFields/taskDraft に集約する。HTTP/Problem は api/http.ts、mutation は csrfToken と X-CSRF-Token を使用する。

Session token を Web storage に置かない。Project/Run/Task ID はサーバーが返した値を使い、UUID や過去 Run の関連を画面で再導出しない。

## 開発と検証

Node.js 26 と pnpm 11.7.0 を使用する。install、API proxy、三語確認と検証コマンドは[ローカル開発](../../docs/development/local-development.md#web)に集約する。

変更時は typecheck、Vitest、build と対象 UI のブラウザ確認を行う。generated iframe/Host API と Task Flow は未実装なので、API client の型や設計だけで利用可能と判断しない。[AGENTS.md](../AGENTS.md) の strict type、JSDoc、Effect cleanup 規約に従う。
