# 配備用 image archive

[export-images.ps1](../scripts/export-images.ps1) の出力 tar を配置する directory。既定名は `projectmind-images.tar`。tar は生成物であり、設計文書ではない。

書き出し、転送、backup と `make deploy` の手順は[起動・image 移送](../../docs/operations/quickstart.md#image-移送と更新)、障害時は[Runbook](../../docs/operations/runbook.md)を参照する。

更新前に旧 archive と実 image ID を別途保持し、新 archive に必要な Backend/Web と依存 image が含まれるか確認する。同じ tag は同じ内容の証明ではない。make deploy は旧 app image の削除後に load して全 service を起動するため、archive の失敗時に自動 rollback はしない。

archive は DB/blob/workspace/KEK の backup ではない。[復元点の構成](../../docs/operations/runbook.md#一致恢复点包含什么)と[互換性審査](../../docs/operations/runbook.md#迁移与回退审查)を済ませ、生成 tar や Secret を文書ブラウザへ埋め込まない。
