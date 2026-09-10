import type { CreateProjectInput, ProjectRecord, UpdateProjectInput } from '../api'

/** 画面で編集する非機密 metadata。settings は変更せず PATCH からも省略する。 */
export interface ProjectDraft { key: string; name: string; description: string; retentionDays: string }
/** 元の要求と採用した送信版を分離し、衝突後も元値を比較できるようにする。 */
export interface ProjectIntent {
  action: 'create' | 'edit' | 'archive' | 'restore' | 'delete'
  original: ProjectRecord | null
  base: ProjectRecord | null
  draft: ProjectDraft
}
/** 未選択でも新規作成できる platform 草稿。 */
export function projectDraft(project: ProjectRecord | null = null): ProjectDraft {
  return { key: project?.key ?? '', name: project?.name ?? '', description: project?.description ?? '', retentionDays: String(project?.retention_days ?? 90) }
}
/** Native form を迂回した event でも公開 field の範囲外を送信しない。 */
export function validProjectDraft(draft: ProjectDraft, creating: boolean): boolean {
  const retention = Number(draft.retentionDays)
  return (!creating || /^[a-z0-9][a-z0-9-]{0,99}$/.test(draft.key))
    && [...draft.name.trim()].length >= 1 && [...draft.name].length <= 200
    && [...draft.description].length <= 4000
    && Number.isInteger(retention) && retention >= 1 && retention <= 3650
}
/** 公開 DTO だけを複製し、列表の更新で確認対象を差し替えない。 */
export function projectIntent(action: ProjectIntent['action'], original: ProjectRecord | null, draft = projectDraft(original)): ProjectIntent {
  const snapshot = original ? structuredClone(original) : null
  return { action, original: snapshot, base: snapshot, draft: { ...draft } }
}
/** 新規作成は原 key を保持し、key 衝突を成功として扱わない。 */
export function projectCreateInput(draft: ProjectDraft): CreateProjectInput {
  return { key: draft.key, name: draft.name, description: draft.description, retention_days: Number(draft.retentionDays), settings: {} }
}
/** settings は未編集なので送らず、現在の Server 設定を上書きしない。 */
export function projectUpdateInput(intent: ProjectIntent): UpdateProjectInput {
  if (!intent.base) throw new Error('Project update requires its original identity')
  return { expected_row_version: intent.base.row_version, name: intent.draft.name, description: intent.draft.description, retention_days: Number(intent.draft.retentionDays) }
}
