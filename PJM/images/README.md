# 配備用 image archive

[export-images.ps1](../scripts/export-images.ps1) が生成する tar の配置先。既定名は `projectmind-images.tar`。archive は生成物であり、設計文書やデータ backup ではない。

## 作成から転送まで

| 段階 | 確認すること |
| --- | --- |
| 作成前 | 対象 context path/config で image を build する。export 自体は build/pull しない |
| 書き出し | application image 不足は失敗、第三者 image 不足は警告して除外される。正常終了だけでは完全な offline 配備一式を保証しない |
| 保管 | archive と実 image ID の対応を記録する。同名 tag や同名 tar を内容の同一性と扱わない |
| 転送・更新 | 送受信の checksum を比較し、承認済み保管元と必要 image を確認する。checksum の一致は配布元の真正性やデータ復元の証明ではない |

コマンドは[image 移送](../../docs/operations/quickstart.md#image-移送と更新)を参照する。同名 archive は既定で上書きされない。別名の `ArchiveName` で前後の版を保持し、`-Force` は意図した上書き時だけ使う。書き出し失敗時に不完全 tar は除去されるため、唯一の回退用 archive を上書き対象にしない。

## 配備と復旧の境界

`make deploy` は旧 app image を削除してから load し、全 service を起動する。archive が必要 image を欠く場合など、途中失敗時に元版へ自動復旧しない。[公開・移行](../../docs/operations/deployment.md)で停写と放行を分けて確認する。

archive は DB/blob/workspace/KEK を含む[同一復元点](../../docs/operations/backup-recovery.md#一致恢复点包含什么)の代わりにならない。[互換性審査](../../docs/operations/deployment.md#迁移与回退审查)を済ませ、tar や Secret を文書ブラウザへ埋め込まない。
