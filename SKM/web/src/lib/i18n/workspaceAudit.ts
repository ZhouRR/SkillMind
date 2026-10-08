/** 実行画面の可読性・復旧状態に使う三語共通メッセージ。 */
export interface WorkspaceAuditMessages {
  proposalEditRejected: string
  proposalOriginal: string
  proposalRequestKey: string
  proposalDecision: string
  proposalSending: string
  proposalConfirmed: string
  proposalUnknown: string
  proposalUnknownHint: string
  proposalCheck: string
  proposalChecking: string
  proposalObserved: string
  proposalConflict: string
  proposalRejected: string
  proposalReadFailed: string
  proposalPending: string
  proposalReadOnly: string
  proposalStorageUnavailable: string
  proposalChanges: string
  toolArguments: string
  toolStatusSucceeded: string
  toolStatusFailed: string
  toolStatusRunning: string
  toolStatusPending: string
  toolStatusCancelled: string
  toolStatusUnknown: string
  detailRetry: string
  detailUpdating: string
  detailStale: string
  reportFallback: string
  reportRetry: string
  invalidJson: string
  updating: string
  viewAllPending: string
  pendingTruncated: string
  readOnlyLaunch: string
  modulesFailed: string
  moduleUnavailable: string
  requestUnavailable: string
  deletionLoading: string
}

export const WORKSPACE_AUDIT_ZH: WorkspaceAuditMessages = {
  proposalEditRejected: "修改被拒绝的决定",
  "proposalOriginal": "原审批请求",
  "proposalRequestKey": "请求标识",
  "proposalDecision": "原决定",
  "proposalSending": "正在提交审批决定",
  "proposalConfirmed": "审批决定已保存",
  "proposalUnknown": "审批结果未知",
  "proposalUnknownHint": "请求可能已被接受。请先读取审批记录核对；核对不会重新发送决定或执行写入。",
  "proposalCheck": "核对审批记录",
  "proposalChecking": "正在读取审批记录",
  "proposalObserved": "已找到相同提案版本的匹配决定。此记录不包含原请求标识，因此无法据此确认原请求回执。",
  "proposalConflict": "此提案版本已有其他决定，请核对记录。原请求不会再次发送。",
  "proposalRejected": "审批请求被拒绝，请核对错误与当前记录。",
  "proposalReadFailed": "未能读取审批记录，原请求仍保留。",
  "proposalPending": "尚未找到匹配的审批记录。原请求结果仍未知，请稍后再次核对。",
  "proposalReadOnly": "此项目为只读，无法提交新审批决定；仍可核对原请求。",
  "proposalStorageUnavailable": "无法保存本页的原请求记录。请保留请求标识，在核对前不要关闭或刷新页面。",
  "proposalChanges": "提案差异与前提条件",
  "toolArguments": "查看完整参数摘要",
  "toolStatusSucceeded": "成功",
  "toolStatusFailed": "失败",
  "toolStatusRunning": "执行中",
  "toolStatusPending": "待执行",
  "toolStatusCancelled": "已取消",
  "toolStatusUnknown": "未知",
  "detailRetry": "重试读取详情",
  "detailUpdating": "正在更新执行详情",
  "detailStale": "详情更新失败，以下为上次读取的内容。",
  "reportFallback": "报告排版失败，以下保留原始结果。",
  "reportRetry": "重试报告排版",
  "invalidJson": "请输入完整、有效的 JSON；原始输入会保留。",
  "updating": "正在更新列表，当前内容仍可查看。",
  "viewAllPending": "查看全部待处理",
  "pendingTruncated": "仅显示前 10 条，另有待处理执行。",
  "readOnlyLaunch": "此项目为只读，无法启动新执行。仍可查看记录并核对原请求。",
  "modulesFailed": "模块范围读取失败，请重试。",
  "moduleUnavailable": "所选模块不可用，请检查模块设置。",
  "requestUnavailable": "正在读取原始请求；当前尚无法显示其完整输入与来源。",
  "deletionLoading": "正在读取此执行的删除影响。"
}

export const WORKSPACE_AUDIT_JA: WorkspaceAuditMessages = {
  proposalEditRejected: "拒否された判断を修正",
  "proposalOriginal": "元の承認要求",
  "proposalRequestKey": "要求 ID",
  "proposalDecision": "元の判断",
  "proposalSending": "承認判断を送信しています",
  "proposalConfirmed": "承認判断を保存しました",
  "proposalUnknown": "承認結果が不明です",
  "proposalUnknownHint": "要求が受理された可能性があります。まず承認記録を確認してください。確認では判断の再送信や書き込みを行いません。",
  "proposalCheck": "承認記録を確認",
  "proposalChecking": "承認記録を読み込んでいます",
  "proposalObserved": "同じ提案バージョンに一致する判断が見つかりました。この記録に元の要求 ID は含まれないため、元要求の受付は確認できません。",
  "proposalConflict": "この提案バージョンには別の判断が記録されています。記録を確認してください。元の要求は再送信しません。",
  "proposalRejected": "承認要求が拒否されました。エラーと現在の記録を確認してください。",
  "proposalReadFailed": "承認記録を読み込めませんでした。元の要求は保持しています。",
  "proposalPending": "一致する承認記録はまだありません。元の要求の結果は不明です。後でもう一度確認してください。",
  "proposalReadOnly": "このプロジェクトは読み取り専用のため、新しい承認判断は送信できません。元の要求は確認できます。",
  "proposalStorageUnavailable": "このページの元の要求を保存できません。要求 ID を控え、確認するまでページを閉じたり再読み込みしたりしないでください。",
  "proposalChanges": "提案の差分と前提条件",
  "toolArguments": "パラメーター概要をすべて表示",
  "toolStatusSucceeded": "成功",
  "toolStatusFailed": "失敗",
  "toolStatusRunning": "実行中",
  "toolStatusPending": "実行待ち",
  "toolStatusCancelled": "キャンセル済み",
  "toolStatusUnknown": "不明",
  "detailRetry": "詳細を再読み込み",
  "detailUpdating": "実行の詳細を更新しています",
  "detailStale": "詳細を更新できませんでした。前回読み込んだ内容を表示しています。",
  "reportFallback": "レポートの整形に失敗しました。元の結果を表示しています。",
  "reportRetry": "レポートの整形を再試行",
  "invalidJson": "有効な JSON を完成させてください。編集中の原文は保持されます。",
  "updating": "一覧を更新中です。現在の内容は引き続き参照できます。",
  "viewAllPending": "対応待ちをすべて表示",
  "pendingTruncated": "最初の 10 件を表示しています。他にも対応待ちがあります。",
  "readOnlyLaunch": "このプロジェクトは読み取り専用のため、新しい実行は開始できません。記録の閲覧と元要求の確認は可能です。",
  "modulesFailed": "モジュール範囲を読み込めませんでした。再試行してください。",
  "moduleUnavailable": "選択したモジュールは利用できません。設定を確認してください。",
  "requestUnavailable": "元の要求を読み込んでいます。入力と参照元の全記録はまだ表示できません。",
  "deletionLoading": "この実行の削除範囲を確認しています。"
}

export const WORKSPACE_AUDIT_EN: WorkspaceAuditMessages = {
  proposalEditRejected: "Edit refused decision",
  "proposalOriginal": "Original approval request",
  "proposalRequestKey": "Request ID",
  "proposalDecision": "Original decision",
  "proposalSending": "Submitting approval decision",
  "proposalConfirmed": "Approval decision saved",
  "proposalUnknown": "Approval result unknown",
  "proposalUnknownHint": "The request may have been accepted. Check the approval record first. Checking will not resend the decision or perform a write.",
  "proposalCheck": "Check approval record",
  "proposalChecking": "Reading approval record",
  "proposalObserved": "A matching decision was found for the same proposal version. This record does not contain the original request ID, so it cannot confirm that request's receipt.",
  "proposalConflict": "A different decision is recorded for this proposal version. Review the record. The original request will not be resent.",
  "proposalRejected": "The approval request was refused. Review the error and current record.",
  "proposalReadFailed": "Could not read the approval record. The original request is preserved.",
  "proposalPending": "No matching approval record was found. The original request's result is still unknown. Check again later.",
  "proposalReadOnly": "This project is read-only. New approval decisions are unavailable; existing requests can still be checked.",
  "proposalStorageUnavailable": "The original request could not be saved for this page. Keep the request ID and do not close or refresh the page before checking it.",
  "proposalChanges": "Proposal changes and preconditions",
  "toolArguments": "View full argument summary",
  "toolStatusSucceeded": "Succeeded",
  "toolStatusFailed": "Failed",
  "toolStatusRunning": "Running",
  "toolStatusPending": "Pending",
  "toolStatusCancelled": "Cancelled",
  "toolStatusUnknown": "Unknown",
  "detailRetry": "Retry loading details",
  "detailUpdating": "Updating run details",
  "detailStale": "Could not update details. Showing the last loaded content.",
  "reportFallback": "Report formatting failed. The original result is preserved below.",
  "reportRetry": "Retry report formatting",
  "invalidJson": "Complete the valid JSON. Your draft is preserved.",
  "updating": "Updating the list. Current items remain available.",
  "viewAllPending": "View all pending",
  "pendingTruncated": "Showing the first 10 items; more runs need attention.",
  "readOnlyLaunch": "This project is read-only. New runs are unavailable; records and original-request checks remain accessible.",
  "modulesFailed": "Could not load the module scope. Please retry.",
  "moduleUnavailable": "The selected module is unavailable. Check its settings.",
  "requestUnavailable": "The original request is not yet available. Its full inputs and sources cannot be shown.",
  "deletionLoading": "Checking what this run deletion affects."
}
