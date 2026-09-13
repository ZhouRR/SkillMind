import { isUuid } from './validation'

/** 公開 sources 契約の文書範囲。ID の生成や server fingerprint の再導出は行わない。 */
export type DocumentSelectionMode = 'SINGLE' | 'SET' | 'ALL'

/** 文書 form の未選択/編集中も表せる読み取り結果。無効値を全集へ丸めない。 */
export interface DocumentSelection {
  mode: DocumentSelectionMode | 'NONE' | 'INVALID'
  ids: string[]
  valid: boolean
}

export const MAX_SELECTED_DOCUMENTS = 5000
export const ALL_DOCUMENTS_SELECTION = 'project-documents:all'
export const PROJECT_DOCUMENT_LIBRARY_SELECTION = 'project-library:documents'

/** Catalog が返す全候補の相対パスから、祖先を含む選択可能なフォルダーを作る。 */
export function documentFolders(options: Array<{ value: string; label: string }>): Array<{ path: string; ids: string[]; value: string }> {
  const folders = new Map<string, string[]>()
  for (const option of options) {
    const selection = readDocumentSelection(option.value)
    if (selection.mode !== 'SINGLE' || !selection.valid) continue
    const parts = option.label.split('/')
    for (let depth = 0; depth < parts.length; depth++) {
      const path = depth === 0 ? '/' : parts.slice(0, depth).join('/')
      const ids = folders.get(path) ?? []
      ids.push(selection.ids[0]!)
      folders.set(path, ids)
    }
  }
  return [...folders].sort(([left], [right]) => left.localeCompare(right)).map(([path, ids]) => ({
    path, ids, value: `${ids.length === 1 ? 'document' : 'documents'}:${ids.join(',')}`,
  }))
}

/** 表示だけで集合を照合する。原 token の順序や草稿は書き換えない。 */
export function sameDocumentMembers(left: string[], right: string[]): boolean {
  const members = new Set(left.map((id) => id.toLowerCase()))
  return left.length === right.length && right.every((id) => members.has(id.toLowerCase()))
}

/** API の文書 UUID を shape として確認するだけで、所有権は server が検証する。 */
export function isDocumentId(value: unknown): value is string {
  return isUuid(value)
}

/** 同一 request の本文はこの処理で変更しない。草稿の選択 token を UI に読み取る。 */
export function readDocumentSelection(value: string): DocumentSelection {
  if (value === '') return { mode: 'NONE', ids: [], valid: true }
  if (value === ALL_DOCUMENTS_SELECTION) return { mode: 'ALL', ids: [], valid: true }
  const mode = value.startsWith('document:') ? 'SINGLE' : value.startsWith('documents:') ? 'SET' : 'INVALID'
  if (mode === 'INVALID') return { mode, ids: [], valid: false }
  const raw = value.slice(value.indexOf(':') + 1)
  const ids = raw === '' ? [] : raw.split(',')
  return {
    mode, ids,
    valid: ids.every(isDocumentId)
      && new Set(ids.map((id) => id.toLowerCase())).size === ids.length
      && (mode === 'SINGLE' ? ids.length === 1 : ids.length >= 2 && ids.length <= MAX_SELECTED_DOCUMENTS),
  }
}

/** 現在の合法候補と照合し、不正/消えた選択を空値や別の文書へ置き換えない。 */
export function validDocumentSelection(
  value: string,
  requirement: { required: boolean; options: Array<{ value: string }> },
): boolean {
  const selection = readDocumentSelection(value)
  if (!selection.valid) return false
  if (selection.mode === 'NONE') return !requirement.required
  if (selection.mode === 'ALL') return requirement.options.some((option) => option.value === value)
  const available = new Set(requirement.options.flatMap((option) => {
    const parsed = readDocumentSelection(option.value)
    return parsed.mode === 'SINGLE' && parsed.valid ? parsed.ids.map((id) => id.toLowerCase()) : []
  }))
  return selection.ids.every((id) => available.has(id.toLowerCase()))
}
