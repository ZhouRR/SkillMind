import type { ProjectModuleRecord, PublishedTaskRecord } from '../api'
import { validDocumentSelection } from './documentSelection'

/** 一つの Run 下書き。「どの task を、どの入力と来源で走らせるか」だけを持つ。
 *
 * 即時実行(工作空间)と時刻起動(任务中心)が同じ形を使う。二つの画面が別々の形を持つと、
 * 片方だけ直された設定が生まれ、どちらが実際に走ったのか読めなくなる。
 */
export interface TaskDraft {
  skillVersionId: string
  taskKey: string
  taskTitle: string
  input: Record<string, unknown>
  sources: Record<string, string>
}

/** Run preflight selector に表示する requirement と具体候補。 */
export interface SourceRequirementChoice {
  key: string
  /** 資源種別(issue/repository/document)。友好名の索引に使う。 */
  kind: string
  access: string
  required: boolean
  options: Array<{ value: string; label: string }>
}

/** Catalog 内で task を一意に識別する合成 key（精確 version + task key）。 */
export function taskCatalogId(task: PublishedTaskRecord): string {
  return `${task.skill_version_id}::${task.task_key}`
}

/** Run preflight の資源要求を readiness(= blueprint 資源要求)から作る。
 *
 * 宣言元は blueprint 一本のため、readiness が未配線(null)の環境では選択肢も出さない。
 * 以前は manifest `data_sources` へ退避していたが、二重宣言が key ずれの原因だった。
 */
export function sourceRequirements(task: PublishedTaskRecord): SourceRequirementChoice[] {
  return (task.readiness?.requirements ?? []).map((requirement) => ({
    key: requirement.key,
    kind: requirement.kind,
    access: requirement.access,
    required: requirement.required,
    options: requirement.candidates.map((candidate) => ({
      value: candidate.key,
      label: requirement.kind === 'document' ? candidate.label : `${candidate.label} · ${candidate.provider}`,
    })),
  }))
}

/** 入力文書だけに単体/集合/全集を使う。成果保存先を入力の全集へ変換しない。 */
export function usesDocumentSelection(requirement: Pick<SourceRequirementChoice, 'kind' | 'access'>): boolean {
  return requirement.kind === 'document' && requirement.access === 'read'
}

/** 文書は候補が一件でも同意を省略しない。従来の必須 Integration 既定だけを維持する。 */
export function defaultSourceProviders(task: PublishedTaskRecord): Record<string, string> {
  const sources: Record<string, string> = {}
  for (const requirement of sourceRequirements(task)) {
    const preferred = requirement.options[0]
    if (requirement.kind !== 'document' && requirement.required && preferred) sources[requirement.key] = preferred.value
  }
  return sources
}

/** 未選択(空文字)の資源要求を落とし、実際に凍結する来源だけを残す。
 *
 * 即時実行と時刻起動が同じ投影を使う。片方だけ空文字を送ると、server 側で
 * 「未知の資源 key」として弾かれる側とされない側が生まれる。
 */
export function selectedSourceMap(providers: Record<string, string>): Record<string, string> {
  const sources: Record<string, string> = {}
  for (const [key, provider] of Object.entries(providers)) {
    if (provider) sources[key] = provider
  }
  return sources
}

/** Form と raw JSON editor が同じ source of truth を共有できる場合だけ object を返す。 */
export function parseInputObject(value: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(value) as unknown
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

/** 選択済み task と入力から Run 下書きを組み立てる。不正 JSON や未確認/失効文書は null。
 *
 * 読めない入力のまま凍結させないのが要点。時刻起動では「保存できたのに最初の発火で必ず落ちる」
 * schedule がそれで生まれる。
 */
export function buildTaskDraft(
  task: PublishedTaskRecord | null,
  inputText: string,
  sourceProviders: Record<string, string>,
): TaskDraft | null {
  if (!task) return null
  const input = parseInputObject(inputText)
  if (input === null) return null
  if (sourceRequirements(task).some((requirement) => {
    if (requirement.kind !== 'document') return false
    const value = sourceProviders[requirement.key] ?? ''
    if (usesDocumentSelection(requirement)) return !validDocumentSelection(value, requirement)
    if (requirement.access !== 'write') return true
    return value === '' ? requirement.required : !requirement.options.some((option) => option.value === value)
  })) return null
  return {
    skillVersionId: task.skill_version_id,
    taskKey: task.task_key,
    taskTitle: task.title,
    input,
    sources: selectedSourceMap(sourceProviders),
  }
}

/** sidebar で選択された業務模块に task catalog を絞る。
    null は模块を持たない Project(絞り込み無し)で、公開済み task を全件返す。 */
export function filterTasksByModule(
  tasks: PublishedTaskRecord[],
  module: ProjectModuleRecord | null,
): PublishedTaskRecord[] {
  if (module === null) return tasks
  return tasks.filter((task) =>
    module.skills.some((skill) => skill.skill_version_id === task.skill_version_id),
  )
}
