import { describe, expect, it } from 'vitest'

import { ALL_DOCUMENTS_SELECTION, readDocumentSelection, validDocumentSelection } from '../../src/lib/documentSelection'
import { buildTaskDraft, defaultSourceProviders, sourceRequirements } from '../../src/lib/taskDraft'
import { DOCUMENT_IDS, documentTask } from '../fixtures/documentTask'

const [first, second] = DOCUMENT_IDS

describe('explicit document scopes', () => {
  it.each([`document:${first}`, `documents:${second},${first}`, ALL_DOCUMENTS_SELECTION])('keeps a valid selection unchanged: %s', (selection) => {
    // 確認済みの文字列は並び替えず、server が意図と凍結内容を別々に正規化する。
    expect(readDocumentSelection(selection).valid).toBe(true)
    expect(buildTaskDraft(documentTask(), '{"objective":"Review"}', { documents: selection })?.sources).toEqual({ documents: selection })
  })

  it('never defaults a required document, including a sole candidate', () => {
    // 候補順が全集から始まっていても暗黙の同意を作らない。
    const task = documentTask()
    expect(defaultSourceProviders(task)).toEqual({})
    task.readiness!.requirements[0]!.candidates = task.readiness!.requirements[0]!.candidates.slice(1, 2)
    expect(defaultSourceProviders(task)).toEqual({})
    expect(buildTaskDraft(task, '{}', {})).toBeNull()
  })

  it('allows explicitly unused optional scope but rejects an unfinished set', () => {
    expect(buildTaskDraft(documentTask(false), '{}', { documents: '' })?.sources).toEqual({})
    expect(buildTaskDraft(documentTask(false), '{}', { documents: 'documents:' })).toBeNull()
    expect(buildTaskDraft(documentTask(), '[]', { documents: ALL_DOCUMENTS_SELECTION })).toBeNull()
  })

  it.each(['document:', 'documents:', `documents:${first}`, `document:${first},${second}`, `documents:${first},${first}`, 'project-documents:other', 'document:not-an-id'])('rejects incomplete or malformed scopes: %s', (selection) => {
    expect(buildTaskDraft(documentTask(), '{}', { documents: selection })).toBeNull()
  })

  it('rejects unavailable members or ALL without replacing the saved choice', () => {
    const requirement = sourceRequirements(documentTask())[0]!
    requirement.options = requirement.options.filter((option) => option.value === `document:${first}`)
    expect(validDocumentSelection(`documents:${first},${second}`, requirement)).toBe(false)
    expect(validDocumentSelection(ALL_DOCUMENTS_SELECTION, requirement)).toBe(false)
    expect(validDocumentSelection(`document:${first}`, requirement)).toBe(true)
  })

  it('checks the 5000 member bound and case-insensitive duplicate UUIDs', () => {
    const ids = Array.from({ length: 5001 }, (_, index) => `00000000-0000-4000-8000-${index.toString(16).padStart(12, '0')}`)
    expect(readDocumentSelection(`documents:${ids.slice(0, 5000).join(',')}`).valid).toBe(true)
    expect(readDocumentSelection(`documents:${ids.join(',')}`).valid).toBe(false)
    const id = '00000000-0000-4000-8000-0000000000ab'
    expect(readDocumentSelection(`documents:${id},${id.toUpperCase()}`).valid).toBe(false)
  })
})
