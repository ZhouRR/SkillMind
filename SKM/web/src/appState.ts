import type { SkillmindMeta, ProjectRecord } from './api'

/** Metadata 読み込み中・成功・失敗を排他的に表す画面 state。 */
export type MetaState =
  | { status: 'loading' }
  | { status: 'ready'; meta: SkillmindMeta }
  | { status: 'error'; message: string }

/** 認証済み actor が参照できる Project list の読み込み state。 */
export type ProjectState =
  | { status: 'idle' | 'loading' }
  | { status: 'ready'; projects: ProjectRecord[] }
  | { status: 'error'; message: string }
