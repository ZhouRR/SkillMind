/** 共有管理画面の補助文案。技術失敗を空の資産一覧へ置き換えない。 */
export interface SharedAuditMessages {
  skillOptionsFailed: string
  moduleHelp: string
  memberHelp: string
}
export const SHARED_AUDIT_ZH: SharedAuditMessages = {
  skillOptionsFailed: '读取可选技能失败。原选择已保留，请重新读取。',
  moduleHelp: '模块如何使用技能', memberHelp: '成员与权限说明',
}
export const SHARED_AUDIT_JA: SharedAuditMessages = {
  skillOptionsFailed: '選択可能な Skill を取得できませんでした。元の選択を保持しています。再読込してください。',
  moduleHelp: 'Module と Skill の関係', memberHelp: 'メンバーと権限について',
}
export const SHARED_AUDIT_EN: SharedAuditMessages = {
  skillOptionsFailed: 'Could not load available skills. Your selection is retained. Reload to continue.',
  moduleHelp: 'How modules use skills', memberHelp: 'Members and permissions',
}
