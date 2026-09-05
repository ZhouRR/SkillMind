# JAF 共通設定・規約

> このファイルは JAF 全 Skillが参照する共通設定と規約を定義する。

---

## ソースパス規約

- メインソースルートディレクトリ：`/jaf_project/src/trunk`

### アプリ名とソースパス

| グループ |アプリ名 | パス | 主言語 |
| ------------| --------------------------------- | ------------------ | ------ |
| 受付G | 受付アプリ | `01.UKT` | C# |
| 指令G | 指令アプリ | `06.SRI` | C# |
| 自動指令G | 自動指令アプリ | `07.JSR` | Java |
| 車載G | 車両動態管理アプリ | `08.SDK` | Java |
| 車載G | スマホ指令アプリ | `09.SHS` | Java |
| 指令G | 基地業務アプリ | `10_KIT` | C# |
| 車載G | 車両業務アプリ | `11.SYG` | Kotlin/Java |
| 車載G | 乗務予定アプリ | `12.JMY` | Java |
| 車載G | 車載連携アプリ | `13.SYR` | Java |
| 受付G | マスターメンテアプリ | `15.MST` | C# |
| 車載G | 外部連携(対JAF内システム)アプリ | `17.GRJ` | Java |
| ApacheCamel | 外部連携(対外業務連携)アプリ | `18.GRG` | Java |
| 指令G | ランチャーアプリ | `19.LNC` | C# |
| 受付G | 損保データ連携アプリ | `26.SDR` | Java |

---

## 技術スタック概要

### Java (Spring Boot + MyBatis)
- JAFRSES 系列: SDK, SHS, JMY, GRJ, GRG, JSR, SDR
- Spring Boot + MyBatis + PostgreSQL

### Kotlin (Android)
- JAFRSHO 系列: SYG, SYR
- MVVM (ViewModel + UseCase + Repository) + Room DB

| 項目 | 内容 |
|------|------|
| 言語 | Kotlin |
| アーキテクチャ | MVVM + Clean Architecture |
| DI | Hilt (Dagger) |
| UI | Jetpack Compose |
| DB | Room (SQLite) |
| ビルド | Gradle (Kotlin DSL) |
| 画面パターン | Action → ViewModel → UseCase → Repository → DataSource |

### C# (WPF + .NET Framework)
- JAFRSCO / JAFRSAO 系列: UKT, SRI, MST, LNC, COM
- WPF + MVVM (GyomuWinMng 基底) + SqlConnection

| 項目 | 内容 |
|------|------|
| 言語 | C# |
| UI フレームワーク | WPF (Windows Presentation Foundation) |
| アーキテクチャ | MVVM |
| 基底クラス | `GyomuWinMng`（業務Window管理） |
| リアクティブ | `ReactiveProperty` / `Reactive.Bindings` |
| JSON | `System.Text.Json` |
| DI | `Microsoft.Extensions.DependencyInjection` |
| ORM | 独自データアクセス層（DataTable/DataSet ベース） |

## C# プロジェクト構造パターン

```
{アプリ名}/{機能ID}/
├── ViewModels/
│   └── JAFRSCO25020ViewModel.cs   ← ViewModel（GyomuWinMng継承）
├── Views/
│   └── JAFRSCO25020View.xaml      ← XAML ビュー
├── Models/
│   └── JAFRSCO25020Model.cs       ← データモデル
├── Dto/
│   └── *Dto.cs                    ← データ転送オブジェクト
├── Controller/                    ← コントローラークラス（地図等）
├── Enum/                          ← 列挙型
├── Utils/                         ← ユーティリティ
└── Common/
    └── JAFRSCOCommonConst.cs      ← 定数定義
```

---

## Android プロジェクト構造

```
{アプリ}/syg/
├── app/           ← アプリ本体
├── core/          ← 共通モジュール（common/designsystem/ui）
├── data/          ← データ層（Repository実装、API通信、Room DB）
├── domain/        ← ドメイン層（UseCase、Model、Repository interface）
├── feature/       ← 機能層（画面機能モジュール群）
└── docs/          ← ドキュメント
```

各画面機能の構成：
```
jafrshoNNNNN/
├── JAFRSHO01010Action.kt    ← sealed class
├── JAFRSHO01010ViewModel.kt ← @HiltViewModel
├── JAFRSHO01010UiState.kt   ← data class
├── JAFRSHO01010Screen.kt    ← @Composable
├── components/              ← UIコンポーネント
└── mappers/                 ← UiState⇔Domain変換
```

処理フロー：`ユーザー操作 → Action → ViewModel.onAction() → UseCase.invoke() → Repository → API通信/Room DAO → UiState更新 → 画面再描画/遷移`

---

## 設計書パス

ルートディレクトリ：`/jaf_project/doc`

| 設計書工程 | パス |
| ----------- | ---- |
| 03_基本設計 | 21_新設設計書（ON SVN）/03_基本設計`                 |
| 04_機能設計 | `21_新設設計書（ON SVN）/04_機能設計` |
| 05_詳細設計 | 21_新設設計書（ON SVN）/05_詳細設計`                 |
| 06_単体試験書 | `30 詳細設計/02_単体テスト仕様書` |
| 07_単体エビデンス | `50 単体テスト/01_単体テストエビデンス` |
|90_DBA（データベース設計）|`21_新設設計書（ON SVN）/05_詳細設計/90_DBA/02_設計`|

---

## 内部インターフェース一覧

ルートディレクトリ：`/jaf_project/doc`/21_新設設計書（ON SVN）/05_詳細設計

| アプリ | インターフェース設計書パス |
| ----------- | ---- |
| 受付アプリ | 01_受付\02_設計\JAF_RS-24-詳設-006-内部インターフェース一覧.xlsx |
| 指令アプリ | 06_指令\02_設計\JAF_RS-24-詳設-006-内部インターフェース一覧_指令アプリ.xlsx |
| 自動指令アプリ | 07_自指\02_設計\JAF_RS-24-詳設-自指-設計999-01_内部インターフェース一覧.xlsx |
| 車両動態アプリ | 08_車動\02_設計\JAF_RS-24-詳設-999-内部インターフェース一覧_車両動態管理アプリ.xlsx |
| スマホ指令アプリ | 09_ス指\02_設計\JAF_RS-24-詳設-999-内部インターフェース一覧_スマホ指令アプリ.xlsx |
| 基地業務アプリ | 10_基業\02_設計\_削除_JAF_RS-24-詳設-999-内部インターフェース一覧_基地業務アプリ.xlsx |
| 車両業務アプリ | 11_車業\02_設計\JAF_RS-24-詳設-999-内部インターフェース一覧_車両業務アプリ.xlsx |
| 乗務予定アプリ | 12_乗予\02_設計\JAF_RS-24-詳設-999-内部インターフェース一覧_乗務予定アプリ.xlsx |
| 車載連携アプリ | 13_車連\02_設計\JAF_RS-24-詳設-999-内部インターフェース一覧_車載連携アプリ.xlsx |
| マスターメンテアプリ | 15_ＭＭ\02_設計\JAF_RS-24-詳設-006-内部インターフェース一覧.xlsx |
| 外部連携（対JAF内システム）アプリ | 17_連内\02_設計\JAF_RS-24-詳設-999-内部インターフェース一覧.xlsx |
| 外部連携（対外業務連携）アプリ | 18_連外\02_設計\JAF_RS-24-詳設-999-内部インターフェース一覧_外部連携（対外業務連携）アプリ.xlsx |
| 損保連携アプリ | 26_損連\02_設計\JAF_RS-24-詳設-006-内部インターフェース一覧.xlsx |

---

## 送信インターフェース一覧

ルートディレクトリ：`/jaf_project/doc`/21_新設設計書（ON SVN）/05_詳細設計

| アプリ         | インターフェース設計書パス                                   |
| -------------- | ------------------------------------------------------------ |
| 車両業務アプリ | 11_車業\02_設計\JAF_RS-24-詳設-999-送信インターフェース一覧_車両業務アプリ.xlsx |

## ロール設計書

ロール設計書ルートディレクトリ：`/jaf_project/doc`/21_新設設計書（ON SVN）/04_機能設計/00_全体/00_ロール設計書

| アプリ | ロール設計書パス |
| ----------- | ---- |
| 受付アプリ | JAF_RS-24-機設-003-JAFRSA_受付アプリ.xlsx |
| 指令アプリ |  JAF_RS-24-機設-003-JAFRSC_指令アプリ.xlsx |
| 自動指令アプリ | JAF_RS-24-機設-003-JAFRSD_自動指令.xlsx |
| ランチャーアプリ | JAF_RS-24-機設-003-JAFRSP_ランチャーアプリ.xlsx |
| 基地業務アプリ | JAF_RS-24-機設-003-JAFRSG_基地業務アプリ.xlsx |
| 車両業務アプリ | JAF_RS-24-機設-003-JAFRSH_車両業務アプリ.xlsx |
| 乗務予定アプリ | JAF_RS-24-機設-003-JAFRSI_乗務予定アプリ.xlsx |
| マスターメンテアプリ | JAF_RS-24-機設-003-JAFRSL_マスターメンテアプリ.xlsx |
|  損保連携アプリ |  JAF_RS-24-機設-003-JAFRSR_損保データ連携アプリ.xlsx |

---

## コーディング規約
- `/jaf_project/doc`/21_新設設計書（ON SVN）/05_詳細設計/99_共通/02_設計/04.コーディング規約

---

## アプリ実装ガイド
- `/jaf_project/doc`/21_新設設計書（ON SVN）/05_詳細設計/99_共通/02_設計/06.アプリ実装ガイド

---

## 共通部品一覧
- `/jaf_project/doc`/21_新設設計書（ON SVN）/05_詳細設計/99_共通/02_設計/03.共通部品一覧

---
## DB設計書
- `/jaf_project/doc`/21_新設設計書（ON SVN）/04_機能設計/90_DBA/02_設計/00.DB情報管理/00.共通/DB一覧変換.xlsmの【新DB一覧】シートを確認する

- `/jaf_project/doc`/21_新設設計書（ON SVN）/05_詳細設計/90_DBA/02_設計_
　 
　  JAF_RS-24-詳設-003_論理テーブル設計書_トラン_

　 JAF_RS-24-詳設-003_論理テーブル設計書_マスター_
　 
　 JAF_RS-24-詳設-003_論理テーブル設計書_車両トラン
　 
　  JAF_RS-24-詳設-003_論理テーブル設計書_車両マスター

---

## ＳＱＬ文の基本設計書
- /jaf_project/doc/21_新設設計書（ON SVN）/04_機能設計/90_DBA/02_設計/00.DB情報管理/00.共通_

  新DBアクセス一覧_ALL.xlsm

---

## マスタデータ
- /jaf_project/doc/21_新設設計書（ON SVN）/04_機能設計/90_DBA/02_設計/00.DB情報管理/00.共通/03.マスタデータ管理

  JAF_RS-24-詳設-999_マスターテーブルデータ定義書.xlsx

  JAF_RS-24-詳設-999_マスターテーブルデータ定義書_コードネームテーブル.xlsx

---

## UTデータベース（PostgreSql）

- サーバー名： 192.170.10.63
- ポート番号： 5432
- データベース名：rsdb-ukt
- ユーザーID：postgres
- パスワード： abcd123456

---
## ITデータベース（PostgreSql）
- サーバー名： 192.170.10.63
- ポート番号： 5432
- データベース名：rsdb-it
- ユーザーID：postgres
- パスワード： abcd123456

---

## DB種別

| アプリ | DB種別 | 備考 |
|--------|--------|------|
| 車両業務アプリ | SQLite | 専用タブレットで動作、Kotlin |
| その他業務アプリ | PostgreSQL | — |

> データベース設計書ファイル名に「車両」の2文字が含まれている場合は、車両業務アプリ用の SQLite DB に関する設計書である。

---

## 各機能のURL
- /jaf_project/doc/21_新設設計書（ON SVN）/05_詳細設計/99_共通/02_設計/04.コーディング規約/JAF_RS-24-詳設-014_コーディング規約（URLおよびディレクトリ構造編）.xlsx

---

## 関連ナレッジベース

```
 **JAF_source_kb** (`/jaf_project/AI/knowledge/JAF_source_kb/`): ソースコード索引
 **JAF_design_kb** (`/jaf_project/AI/knowledge/JAF_design_kb/`): 設計書MD
 **JAF_handover_kb** (`/jaf_project/AI/knowledge/JAF_handover_kb/`): 引継ぎドキュメント
 **JAF_quality_kb** (`/jaf_project/AI/knowledge/JAF_quality_kb/`): 品質管理
 **JAF_if_analysis_kb** (`/jaf_project/AI/knowledge/JAF_if_analysis_kb/`): IF分析結果（車載連携アプリ全121件）
```


---

## ユーザー設定・運用ルール

### 基本プロフィール
- ## ユーザープロフィール
  日本オフショア開発20年、中国語母語、PG→PM。JAFシステムの開発・保守を担当。
  プロセス重視、ツール重視、実務派。自動化優先、手動操作を排除したい。

  ## コミュニケーションスタイル
  - 日本語で返答、直接的かつ簡潔に、無駄な言葉は不要
  - 不確かな場合は「不確か」と言い、でっち上げない
  - 要件を理解してから着手し、手順を飛ばさない

  ## 作業ルール
  - コードを書く前に既存コードを確認し、無関係な部分をリファクタリングしない
  - ファイルの修正・削除前に計画を説明する
  - .env と credential ファイルを変更しない
  - タスク完了後は grep/count で検証し、「感覚」で完了報告しない
  - 障害分析は証拠ドリブンで、コードとデータベースから根本原因を特定する

  ## 記憶の鉄則
  - パスや値の確認は実検証に基づき、記憶に頼らない
  - JAF 全操作の第一歩：common.md を読み込む
  - 捏造厳禁：未検索を偽るな
  - 「わからない」が沈黙より 100 倍良い

### 自動化方針
- 手動操作は極力排除し、**全自動実行**を前提とする
- 「検証を実行しますか？」のような確認ステップは禁止。常に自動実行
- バグ修正とSkill更新は同時に行う（「同时修改好Skill」）。別々に実行しない

### 出力品質基準
- 批量生成後に全削除されないよう、最初の1件で完璧なテンプレートを提示し、ユーザー承認後に残りを生成
- 完了報告には必ず grep カウント + head 確認結果の具体的数値を添える
- 宣言だけの報告は禁止。「本当に実行したか」は常に検証される
