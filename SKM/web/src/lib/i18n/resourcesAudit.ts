/** 資源管理の読取状態、送信待機と再確認を三語で同じ契約に揃える。 */
export interface ResourcesAuditMessages {
  refresh: string
  refreshing: string
  taskLoading: string
  taskLoadFailed: string
  noTasks: string
  manualTaskEntry: string
  manualTaskHint: string
  saving: string
  working: string
  stopWaiting: string
  waitHint: string
  unknownTitle: string
  unknownHint: string
  reviewComplete: string
  reviewHint: string
  close: string
  connectionSection: string
  authenticationSection: string
  scopeSection: string
  mcpToolCount: (count: number) => string
  scopeNarrowLabel: (name: string) => string
  scopeKeepAllLabel: (name: string) => string
  readTimeout: string
}

export const RESOURCES_AUDIT_ZH: ResourcesAuditMessages = {
  refresh: '重新读取', refreshing: '正在刷新，显示上次确认的配置…',
  taskLoading: '正在读取已发布任务…', taskLoadFailed: '无法读取任务目录。请重新读取，或明确选择使用技术标识。',
  noTasks: '此项目没有已发布任务。', manualTaskEntry: '使用技术标识配置',
  manualTaskHint: '请核对任务范围标识、资源需求标识及能力；这些值未由任务目录确认。',
  saving: '正在保存…', working: '正在处理…', stopWaiting: '停止等待',
  waitHint: '已提交的字段已锁定。停止等待只会停止接收结果，服务器仍可能完成原请求。',
  unknownTitle: '原请求结果尚未确认',
  unknownHint: '没有重发或取消服务器操作。请重新读取并核对列表；配置、凭据等可能已部分保存。核对前不能再次提交。',
  reviewComplete: '已核对当前配置，结束本次提交',
  reviewHint: '读取列表不能证明原请求已结束。请确认实际状态；结束本次提交会清除表单中的凭据值，之后请从最新配置重新编辑。',
  close: '关闭', connectionSection: '连接', authenticationSection: '认证', scopeSection: '访问范围',
  mcpToolCount: (count) => `已发现 ${count} 个工具`, scopeNarrowLabel: (name) => `收窄${name}`,
  scopeKeepAllLabel: (name) => `${name}：保持不限`, readTimeout: '读取超时，请重新读取。',
}

export const RESOURCES_AUDIT_JA: ResourcesAuditMessages = {
  refresh: '再読み込み', refreshing: '更新中です。最後に確認した設定を表示しています…',
  taskLoading: '公開タスクを読み込み中…', taskLoadFailed: 'タスク一覧を読み込めません。再読み込みするか、技術識別子による設定を選んでください。',
  noTasks: 'このプロジェクトに公開タスクはありません。', manualTaskEntry: '技術識別子で設定',
  manualTaskHint: 'タスク範囲・リソース要件の識別子と能力を確認してください。これらの値はタスク一覧で確認されていません。',
  saving: '保存中…', working: '処理中…', stopWaiting: '待機を終了',
  waitHint: '送信した項目をロックしています。待機を終了しても、サーバーは元の要求を完了する場合があります。',
  unknownTitle: '元の要求の結果を確認できません',
  unknownHint: '再送やサーバー処理の取消は行っていません。再読み込みして一覧を確認してください。設定や認証情報の一部が保存済みの場合があります。確認前に再送信はできません。',
  reviewComplete: '現在の設定を確認して今回の送信を閉じる',
  reviewHint: '一覧の読み込みだけでは元の要求の完了を証明できません。実際の状態を確認してください。今回の送信を閉じるとフォームの認証値を消去します。次は最新の設定から編集してください。',
  close: '閉じる', connectionSection: '接続', authenticationSection: '認証', scopeSection: 'アクセス範囲',
  mcpToolCount: (count) => `${count} 件のツールを検出`, scopeNarrowLabel: (name) => `${name}を絞り込む`,
  scopeKeepAllLabel: (name) => `${name}：制限なしを維持`, readTimeout: '読み込みがタイムアウトしました。再読み込みしてください。',
}

export const RESOURCES_AUDIT_EN: ResourcesAuditMessages = {
  refresh: 'Reload', refreshing: 'Refreshing; showing the last confirmed configuration…',
  taskLoading: 'Loading published tasks…', taskLoadFailed: 'The task catalog could not be loaded. Reload it or explicitly choose technical identifiers.',
  noTasks: 'This project has no published tasks.', manualTaskEntry: 'Configure with technical identifiers',
  manualTaskHint: 'Verify the task scope, requirement identifier, and capability. These values have not been confirmed by the task catalog.',
  saving: 'Saving…', working: 'Working…', stopWaiting: 'Stop waiting',
  waitHint: 'Submitted fields are locked. Stopping the wait only stops receiving the result; the server may still complete the original request.',
  unknownTitle: 'The original request outcome is unconfirmed',
  unknownHint: 'The server operation was not retried or cancelled. Reload and check the lists; configuration or credentials may have been partly saved. New submissions are blocked until you check.',
  reviewComplete: 'I checked the current configuration; close this submission',
  reviewHint: 'Reading the lists does not prove that the original request has finished. Verify the actual state. Closing this submission clears credential values in the form; start any further edit from the latest configuration.',
  close: 'Close', connectionSection: 'Connection', authenticationSection: 'Authentication', scopeSection: 'Access scope',
  mcpToolCount: (count) => `${count} discovered tools`, scopeNarrowLabel: (name) => `Narrow ${name}`,
  scopeKeepAllLabel: (name) => `${name}: keep unrestricted`, readTimeout: 'The read timed out. Reload to try again.',
}
