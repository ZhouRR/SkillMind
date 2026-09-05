---
name: repository-review
description: 固定 revision の repository file を 1 つレビューし、evidence に基づく findings を返す。
allowed-tools: [repository.read/v1]
---
# リポジトリレビュー

不変の Git または SVN revision にある repository file を 1 つレビューする。登録済みの
`repository.read/v1` capability だけを使い、repository を変更しない。

## 入力

固定の `revision`、repository 相対の `path`、`review_focus` を必須とする。`review_focus` には
correctness、maintainability、security、またはその他の明示された観点を指定する。欠けている値を
推測で補わない。

## 結果

簡潔な `summary`、`findings` の配列、report が引用した一意な `evidence_refs`、`needs_review` を
返す。各 finding は `severity`、`title`、`description`、`evidence_refs` を持つ。`severity` は
`info`、`warning`、`error` のいずれかとする。

source 内容が不完全な場合、evidence が不十分な場合、結論に人の判断が必要な場合は `needs_review`
を立てる。evidence 識別子を新たに作らず、Tool が返した値だけを再利用する。

## 安全

code、script、test、package manager、shell command を実行しない。外部 system へ書き込まず、
選択した file 以外の repository 内容を露出しない。
