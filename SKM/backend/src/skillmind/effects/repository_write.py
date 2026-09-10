"""repository.write/v1 Proposal の deterministic policy を定義する (計画 §20 R1)。

Agent は既存の `change.propose/v1` しか呼ばない。ここはその提案を「承認後に git へ落とせる形」
へ正規化・検証する層であり、権限は一切与えない。落とす形は **platform 予約 namespace の新規
branch + commit** に限る。既定/保護 branch へは構造的に到達できないため、「承認済みでも既存
branch を書き換えてしまった」という事故が原理的に起きない。

変更表現は「1 file 全文の SET / REMOVE」とする。unified diff ではなく全文を運ぶのは、
read-back 検証 (提案内容の hash と適用後の内容 hash の一致) と idempotent replay 判定を
path 単位で決定的にできるため。差分そのものは base commit と新 commit の間に git が持つ。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.integrations.domain import (
    REPOSITORY_WRITE_MODE_DEFAULT,
    REPOSITORY_WRITE_MODE_DIRECT,
    path_within_scope,
)

REPOSITORY_WRITE_CAPABILITY = "repository.write/v1"
REPOSITORY_WRITE_PROVIDER_VERSION = "git-branch-commit/v1"
REPOSITORY_WRITE_SVN_PROVIDER_VERSION = "svn-branch-commit/v1"

_BRANCH_PATTERN = re.compile(r"^skillmind/[A-Za-z0-9][A-Za-z0-9._/-]{0,110}$")
# direct mode も含めた target 名の共通健全性 (ref として安全か)。namespace の判定は別。
_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_FILE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,4000}$")

# 変更規模の上限 (D4)。人手 review と read-back が成り立つ範囲に限る。超過は截断せず拒否する。
_MAX_CHANGED_FILES = 50
_MAX_FILE_BYTES = 1_048_576
_MAX_TOTAL_BYTES = 4_194_304


def validate_repository_write_proposal(
    draft: ChangeProposalDraft,
    *,
    binding_scope: Mapping[str, Any],
    integration_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Repository 変更提案を凍結 scope・予約 branch・UTF-8 本文の範囲へ限定する。

    ここを通った payload だけが承認と apply の対象になる。scope 外 path、既定 branch、
    binary/過大な内容は提案の段階で閉じる (承認画面に出た時点で「承認すれば通る」ものだけが
    並んでいる状態を保つ)。
    """

    if draft.capability_version != REPOSITORY_WRITE_CAPABILITY:
        raise ChangeProposalValidationError("Effect capability is not repository.write/v1")
    config = integration_config or {}
    mode = config.get("write_mode", REPOSITORY_WRITE_MODE_DEFAULT)
    branch = draft.target.get("locator")
    if not isinstance(branch, str) or _TARGET_PATTERN.fullmatch(branch) is None:
        raise ChangeProposalValidationError("Repository target branch is invalid")
    if mode == REPOSITORY_WRITE_MODE_DIRECT:
        # direct では「設定された既定 branch そのもの」以外へは書かせない。任意 branch を
        # 直接書けるようにすると、予約 namespace という唯一の構造的歯止めが消える。
        if branch != config.get("default_revision"):
            raise ChangeProposalValidationError(
                "Direct write target must be the Integration default branch"
            )
    elif _BRANCH_PATTERN.fullmatch(branch) is None:
        raise ChangeProposalValidationError(
            "Repository target branch must live under the skillmind/ namespace"
        )
    configured_prefix = (integration_config or {}).get("write_branch_prefix")
    if isinstance(configured_prefix, str) and not branch.startswith(configured_prefix):
        # Integration が予約 namespace の内側をさらに狭めている場合、その外は提案段階で閉じる。
        raise ChangeProposalValidationError(
            "Repository target branch is outside the Integration write prefix"
        )
    base_revision = draft.precondition.get("revision")
    if not isinstance(base_revision, str) or not base_revision:
        raise ChangeProposalValidationError("Repository change requires a frozen base revision")

    files: dict[str, str | None] = {}
    total_bytes = 0
    for change in draft.changes:
        path = change.get("path")
        action = change.get("action")
        if not isinstance(path, str) or not path.startswith("/files/"):
            raise ChangeProposalValidationError("Repository change path must stay under /files")
        repository_path = path.removeprefix("/files/")
        if _FILE_PATH_PATTERN.fullmatch(repository_path) is None or ".." in repository_path.split(
            "/"
        ):
            raise ChangeProposalValidationError("Repository change path is invalid")
        value = change.get("value")
        if action == "REMOVE":
            if value is not None:
                raise ChangeProposalValidationError("Repository REMOVE must not carry content")
            files[repository_path] = None
            continue
        if action != "SET":
            # APPEND は「適用済みか」を内容だけから判定できず、replay が二重追記になる。
            raise ChangeProposalValidationError(
                "repository.write/v1 only permits idempotent SET or REMOVE"
            )
        if not isinstance(value, str):
            raise ChangeProposalValidationError("Repository SET requires UTF-8 text content")
        encoded = len(value.encode("utf-8"))
        if encoded > _MAX_FILE_BYTES:
            raise ChangeProposalValidationError("Repository change exceeds the per-file limit")
        total_bytes += encoded
        files[repository_path] = value
    if not files:
        raise ChangeProposalValidationError("Repository change set is empty")
    if len(files) > _MAX_CHANGED_FILES or total_bytes > _MAX_TOTAL_BYTES:
        raise ChangeProposalValidationError("Repository change set exceeds the size limit")

    allowed_paths = [item for item in binding_scope.get("paths", []) if isinstance(item, str)]
    # 書き込み可能な範囲は、その binding が読める範囲と同一とする。読めない場所を書けるのは
    # 「観察してから提案する」という §2.5 の順序が成立しないため。
    if not allowed_paths or any(
        not path_within_scope(path, allowed_paths) for path in files
    ):
        raise ChangeProposalValidationError("Repository change exceeds the frozen binding scope")
    requested_scope = repository_write_scope_from_payload({"files": files})
    return {
        "target_branch": branch,
        "base_revision": base_revision,
        "commit_message": draft.summary,
        "files": files,
        "requested_scope": requested_scope,
    }


def repository_write_scope_from_payload(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    """Provider payload から監査・照合用の変更 path 一覧を返す。

    repository.write は事前許可の対象外 (§20.2) のため、この scope は preauthorization 照合には
    使われない。承認画面と監査 log が「どの path が書かれるのか」を一意に読めるようにするための
    正規化であり、実際の境界判定は `validate_repository_write_proposal` の包含検査が担う。
    """

    files = payload.get("files")
    if not isinstance(files, Mapping) or not files:
        raise ChangeProposalValidationError("Repository write payload is invalid")
    return {"paths": sorted(str(path) for path in files)}
