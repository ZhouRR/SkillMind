import { isValidElement, type ReactElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, loadEvaluationPage, loadEvaluationSubmission, submitEvaluation,
  type EvaluationPage, type EvaluationSubmissionInput, type RunDetailRecord } from '../../src/api'
import { EvaluationSection, RunEvaluations } from '../../src/components/EvaluationSection'
import { RunResultPanel, type RunDetailState } from '../../src/components/RunResultPanel'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { EVALUATION_SCOPE as S, SUBMISSION_KEY as KEY, evaluationReceipt, evaluationResult } from '../fixtures/evaluation'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'
import { interactionDetail } from '../fixtures/interaction'

vi.mock('react', async (original) => ({ ...await original<typeof import('react')>(),
  ...(await import('../fixtures/hookHarness')).hookReact }))
vi.mock('../../src/i18n', async () => ({ useMessages: () => MESSAGES.zh }))
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(),
  loadEvaluationPage: vi.fn(), loadEvaluationSubmission: vi.fn(), submitEvaluation: vi.fn() }))

const labels = MESSAGES.zh.evaluation
const resultLabels = MESSAGES.zh.runResult
const OTHER = '00000000-0000-4000-8000-000000000912'
const expired = vi.fn()
let result = evaluationResult()

/** JSX の公開 handler だけを呼ぶ。DOM の妥当性検証や React の再調停は模倣しない。 */
interface UiProps {
  children?: ReactNode; className?: string; type?: string; value?: string | number
  disabled?: boolean; readOnly?: boolean; role?: string; maxLength?: number
  onChange?: (event: { target: { value: string } }) => void
  onSubmit?: (event: { preventDefault: () => void }) => void
  onClick?: () => void
}
type UiElement = ReactElement<UiProps>
type SectionProps = Parameters<typeof EvaluationSection>[0]

/** 既存 hook runtime で実 component と二つの実 request hook を同じ owner として描く。 */
function render(overrides: Partial<SectionProps> = {}): UiElement {
  hookPhases.cursor = 0
  return EvaluationSection({ scope: S, result, csrfToken: 'synthetic-session', writable: true,
    accessFailure: null, onSessionExpired: expired, ...overrides })
}

/** Timer を進めず、履歴/受付記録の passive effect まで明示的に commit する。 */
async function settle(overrides: Partial<SectionProps> = {}): Promise<UiElement> {
  for (let index = 0; index < 6; index++) { await hookMicrotasks(); render(overrides); commitHooks() }
  return render(overrides)
}

/** Function component を勝手に実行せず、返された JSX の host 配線だけを探索する。 */
function elements(node: ReactNode, predicate: (item: UiElement) => boolean): UiElement[] {
  if (Array.isArray(node)) return node.flatMap((child) => elements(child, predicate))
  if (!isValidElement<UiProps>(node)) return []
  return [...(predicate(node) ? [node] : []), ...elements(node.props.children, predicate)]
}

/** Label/button の文字列だけを取り出し、HTML の文字列照合で操作対象を曖昧にしない。 */
function text(node: ReactNode): string {
  if (Array.isArray(node)) return node.map(text).join('')
  if (isValidElement<UiProps>(node)) return text(node.props.children)
  return typeof node === 'string' || typeof node === 'number' ? String(node) : ''
}

/** 対象が消失/重複した変更は、別の control を黙って操作せず失敗させる。 */
function one(node: ReactNode, predicate: (item: UiElement) => boolean): UiElement {
  const found = elements(node, predicate)
  expect(found).toHaveLength(1)
  return found[0]!
}

/** 実 label に紐付く一つの入力を選ぶ。 */
function field(node: ReactNode, label: string): UiElement {
  const parent = one(node, (item) => item.type === 'label' && text(
    (Array.isArray(item.props.children) ? item.props.children : [item.props.children])
      .filter((child) => !isValidElement(child)),
  ) === label)
  return one(parent, (item) => ['input', 'textarea', 'select'].includes(String(item.type)))
}

/** Form field の React handler に必要な最小の synthetic event を渡す。 */
function change(node: ReactNode, label: string, value: string): void {
  const handler = field(node, label).props.onChange
  expect(handler).toBeTypeOf('function')
  handler!({ target: { value } })
}

/** 無効 button を利用者が押せたことにせず、有効な handler だけを実行する。 */
function click(node: ReactNode, label: string): void {
  const target = one(node, (item) => item.type === 'button' && text(item) === label)
  expect(target.props.disabled).not.toBe(true)
  expect(target.props.onClick).toBeTypeOf('function')
  target.props.onClick!()
}

/** HTML required に頼らず、実 form の全行検証を直接通す。 */
function submitForm(node: ReactNode, className = 'evaluationForm'): void {
  one(node, (item) => item.type === 'form' && item.props.className === className).props.onSubmit!({ preventDefault: vi.fn() })
}

/** 修訂の表示順と送信順を同じ行番号で検証する。 */
function row(node: ReactNode, index: number): UiElement {
  const value = elements(node, (item) => item.props.className === 'evaluationRevisionEditor')[index]
  expect(value).toBeDefined()
  return value!
}

/** 一行ごとに再描画し、古い closure を使う test 側の上書きを避ける。 */
function addRevision(pointer: string, value: string, reason: string): void {
  const index = elements(render(), (item) => item.props.className === 'evaluationRevisionEditor').length
  click(render(), resultLabels.addRevision)
  change(row(render(), index), resultLabels.jsonPointerLabel, pointer)
  change(row(render(), index), resultLabels.suggestedValueLabel, value)
  change(row(render(), index), resultLabels.revisionReasonLabel, reason)
}

/** UUID key と評価草稿は別 form から入力する。 */
function lookup(key = KEY): void {
  change(render(), labels.lookupLabel, key)
  submitForm(render(), 'evaluationLookup')
}

/** 履歴空ページも scope と Result を明示した本来の契約形状にする。 */
function page(items: EvaluationPage['items'] = []): EvaluationPage {
  return { project_id: S.projectId, run_id: S.runId, result_id: S.resultId, items, next_cursor: null }
}

/** 成功 receipt を送信直前の payload から作り、草稿値から再生成しない。 */
function sent(): EvaluationSubmissionInput {
  const input = vi.mocked(submitEvaluation).mock.calls.at(-1)?.[2]
  expect(input).toBeDefined()
  return input!
}

/** Disabled の継承元である form 外側 fieldset を直接検証する。 */
function draftLocked(node: ReactNode): boolean {
  const form = one(node, (item) => item.props.className === 'evaluationForm')
  return one(form.props.children, (item) => item.type === 'fieldset'
    && elements(item, (child) => child.type === 'legend' && text(child) === labels.draft).length === 1).props.disabled === true
}

beforeEach(() => {
  vi.resetAllMocks()
  result = evaluationResult()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
  vi.mocked(loadEvaluationPage).mockResolvedValue(page())
})
afterEach(() => { unmountHooks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('evaluation component draft and original request', () => {
  it('keeps the unknown request visible outside a closed drawer and retains its draft on reopening', async () => {
    vi.mocked(submitEvaluation).mockRejectedValue(new TypeError('offline'))
    render({ open: true }); commitHooks(); await settle({ open: true })
    change(render({ open: true }), resultLabels.commentLabel, 'retained draft')
    submitForm(render({ open: true }))
    await settle({ open: false })
    const closed = render({ open: false })
    const notice = one(closed, (item) => item.props.className === 'evaluationNotice')
    expect(text(notice)).toContain(labels.phase.unknown)
    const reopened = render({ open: true })
    expect(field(reopened, resultLabels.commentLabel).props.value).toBe('retained draft')
    expect(draftLocked(reopened)).toBe(true)
    expect(submitEvaluation).toHaveBeenCalledTimes(1)
  })
  it('keeps ordered revisions, original null/escaped values, JSON suggestions and untrimmed reasons distinct', async () => {
    vi.mocked(submitEvaluation).mockImplementation(async (_project, _run, input) => {
      const receipt = evaluationReceipt(input)
      receipt.evaluation.revisions[1]!.original_value = { a: 1, b: null }
      return receipt
    })
    render(); commitHooks(); await settle()
    change(render(), resultLabels.commentLabel, '  人工判断\n')
    addRevision('/nullable', 'null', '  原値は null のまま  ')
    addRevision('/a~1b/~0value', '{"b":false,"a":2}', '別の構造を提案\n')
    const before = renderToStaticMarkup(render())
    expect(before).toContain('<pre>null</pre>')
    expect(before).toContain('&quot;a&quot;: 1')
    expect(before).toContain('&quot;b&quot;: null')
    submitForm(render())
    const done = await settle()
    expect(sent()).toMatchObject({ result_id: S.resultId, comment: '  人工判断\n', revisions: [
      { pointer: '/nullable', suggested_value: null, reason: '  原値は null のまま  ' },
      { pointer: '/a~1b/~0value', suggested_value: { b: false, a: 2 }, reason: '別の構造を提案\n' },
    ] })
    expect(sent().revisions.every((revision) => !Object.hasOwn(revision, 'original_value'))).toBe(true)
    expect(renderToStaticMarkup(done)).toContain(labels.phase.confirmed)
    expect(renderToStaticMarkup(done)).toContain('data-evaluation-id=')
    expect(draftLocked(done)).toBe(true)
  })

  it.each(['/missing', '/array/01', '/array/-', '/a~2b', '/nullable/child'])(
    'reports the invalid second pointer %s without partially sending the valid first revision', async (pointer) => {
      render(); commitHooks(); await settle()
      addRevision('/nullable', 'null', '合法原値')
      addRevision(pointer, '42', '位置確認')
      submitForm(render())
      const html = renderToStaticMarkup(render())
      expect(html).toContain(`${labels.revisionNumber(2)}: ${labels.draftErrors.invalidPointer}`)
      expect(elements(row(render(), 1), (item) => item.props.className === 'evaluationOriginal')).toHaveLength(0)
      expect(submitEvaluation).not.toHaveBeenCalled()
      expect(draftLocked(render())).toBe(false)
    },
  )

  it.each([
    { pointer: '/nullable', value: 'null', reason: '重複', error: 'duplicatePointer' },
    { pointer: '/summary', value: 'unquoted', reason: '確認', error: 'invalidJson' },
    { pointer: '/summary', value: 'null', reason: '', error: 'invalidReason' },
  ] as const)('keeps the whole draft when the second row has $error', async (bad) => {
    render(); commitHooks(); await settle()
    addRevision('/nullable', 'null', '保留')
    addRevision(bad.pointer, bad.value, bad.reason)
    submitForm(render())
    expect(renderToStaticMarkup(render())).toContain(labels.draftErrors[bad.error])
    expect(elements(render(), (item) => item.props.className === 'evaluationRevisionEditor')).toHaveLength(2)
    expect(submitEvaluation).not.toHaveBeenCalled()
  })

  it('accepts codepoint-limit emoji drafts without the HTML UTF-16 limits truncating legal input', async () => {
    const comment = '😀'.repeat(4000)
    const reason = '😀'.repeat(1000)
    const name = '😀'.repeat(511)
    const pointer = `/${name}`
    result = { ...result, data: { [name]: null } }
    vi.mocked(submitEvaluation).mockImplementation(async (_project, _run, input) => evaluationReceipt(input))
    render(); commitHooks(); await settle()
    change(render(), resultLabels.commentLabel, comment)
    addRevision(pointer, 'null', reason)
    expect(field(render(), resultLabels.commentLabel).props.maxLength).toBeGreaterThanOrEqual(comment.length)
    expect(field(row(render(), 0), resultLabels.jsonPointerLabel).props.maxLength).toBeGreaterThanOrEqual(pointer.length)
    expect(field(row(render(), 0), resultLabels.revisionReasonLabel).props.maxLength).toBeGreaterThanOrEqual(reason.length)
    submitForm(render()); const done = await settle()
    expect(sent().comment).toBe(comment)
    expect(sent().revisions).toEqual([{ pointer, suggested_value: null, reason }])
    expect(renderToStaticMarkup(done)).toContain(labels.phase.confirmed)
  })

  it.each([
    { part: 'comment', count: 4001, error: 'invalidDraft' },
    { part: 'reason', count: 1001, error: 'invalidReason' },
    { part: 'pointer', count: 512, error: 'invalidPointer' },
  ] as const)('rejects an over-limit emoji $part through the codepoint validator without partial submission', async (bad) => {
    const name = bad.part === 'pointer' ? '😀'.repeat(bad.count) : 'nullable'
    result = { ...result, data: { [name]: null } }
    render(); commitHooks(); await settle()
    change(render(), resultLabels.commentLabel, bad.part === 'comment' ? '😀'.repeat(bad.count) : 'valid')
    addRevision(`/${name}`, 'null', bad.part === 'reason' ? '😀'.repeat(bad.count) : 'valid')
    submitForm(render())
    expect(renderToStaticMarkup(render())).toContain(labels.draftErrors[bad.error])
    expect(submitEvaluation).not.toHaveBeenCalled()
    expect(draftLocked(render())).toBe(false)
  })

  it('freezes sent input apart from draft edits and exposes no GET-only exit after a real POST becomes unknown', async () => {
    vi.mocked(submitEvaluation).mockRejectedValue(new TypeError('synthetic transport failure'))
    vi.mocked(loadEvaluationSubmission).mockRejectedValue(new ApiProblemError('private', 404, 'evaluation_submission_not_found'))
    render(); commitHooks(); await settle()
    change(render(), resultLabels.commentLabel, 'original request')
    addRevision('/nullable', 'null', 'original reason')
    submitForm(render()); await settle()
    const original = structuredClone(sent())
    expect(draftLocked(render())).toBe(true)
    expect(field(render(), labels.submissionKey).props.readOnly).toBe(true)
    expect(field(render(), labels.submissionKey).props.value).toBe(original.submission_key)
    // DOM では disabled が防ぐ編集でも、原要求の再送内容は草稿 alias に依存しない。
    change(render(), resultLabels.commentLabel, 'unsent local draft')
    submitForm(render())
    click(render(), labels.confirm); await settle()
    expect(renderToStaticMarkup(render())).toContain(labels.failures.notSeen)
    for (const label of [labels.closeLookup, labels.newEvaluation, labels.editRejected]) {
      expect(elements(render(), (item) => item.type === 'button' && text(item) === label)).toHaveLength(0)
    }
    click(render(), labels.resend); await settle()
    expect(submitEvaluation).toHaveBeenCalledTimes(2)
    expect(sent()).toEqual(original)
    expect(field(render(), resultLabels.commentLabel).props.value).toBe('unsent local draft')
    expect(loadEvaluationSubmission).toHaveBeenCalledTimes(1)
  })

  it('ends a read-only 404 lookup, preserves every draft field and allows a corrected GET key without POST', async () => {
    vi.mocked(loadEvaluationSubmission).mockRejectedValueOnce(new ApiProblemError('private', 404, 'evaluation_submission_not_found'))
      .mockResolvedValueOnce(evaluationReceipt())
    render(); commitHooks(); await settle()
    change(render(), resultLabels.ratingLabel, '5')
    change(render(), resultLabels.verdictLabel, 'accurate')
    change(render(), resultLabels.commentLabel, 'draft survives lookup')
    addRevision('/nullable', 'null', 'draft reason')
    lookup(OTHER); await settle()
    expect(draftLocked(render())).toBe(true)
    click(render(), labels.closeLookup)
    expect(draftLocked(render())).toBe(false)
    expect(field(render(), resultLabels.ratingLabel).props.value).toBe(5)
    expect(field(render(), resultLabels.verdictLabel).props.value).toBe('accurate')
    expect(field(render(), resultLabels.commentLabel).props.value).toBe('draft survives lookup')
    expect(field(row(render(), 0), resultLabels.jsonPointerLabel).props.value).toBe('/nullable')
    expect(field(row(render(), 0), resultLabels.suggestedValueLabel).props.value).toBe('null')
    expect(field(row(render(), 0), resultLabels.revisionReasonLabel).props.value).toBe('draft reason')
    lookup(KEY); const done = await settle()
    expect(renderToStaticMarkup(done)).toContain(labels.phase.confirmed)
    expect(vi.mocked(loadEvaluationSubmission).mock.calls.map((call) => call[3])).toEqual([OTHER, KEY])
    expect(submitEvaluation).not.toHaveBeenCalled()
    click(done, labels.closeLookup)
    expect(field(render(), resultLabels.commentLabel).props.value).toBe('draft survives lookup')
  })

  it('closes an active GET-only lookup synchronously and ignores a late 401 while keeping the draft editable', async () => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(loadEvaluationSubmission).mockReturnValue(response.promise)
    render(); commitHooks(); await settle()
    change(render(), resultLabels.commentLabel, 'preserved')
    lookup(); await settle()
    click(render(), labels.closeLookup)
    response.reject(new ApiProblemError('private', 401))
    const done = await settle()
    expect(vi.mocked(loadEvaluationSubmission).mock.calls[0]![4]?.aborted).toBe(true)
    expect(expired).not.toHaveBeenCalled()
    expect(draftLocked(done)).toBe(false)
    expect(field(done, resultLabels.commentLabel).props.value).toBe('preserved')
    expect(submitEvaluation).not.toHaveBeenCalled()
  })
})

describe('evaluation component read authorization and concurrent history', () => {
  it.each(['history', 'confirmation'] as const)('notifies current %s GET 401 once and closes the draft write gate', async (source) => {
    const history = deferred<EvaluationPage>()
    vi.mocked(loadEvaluationPage).mockReturnValue(history.promise)
    vi.mocked(loadEvaluationSubmission).mockRejectedValue(new ApiProblemError('private', 401))
    render(); commitHooks(); await settle()
    if (source === 'history') history.reject(new ApiProblemError('private', 401))
    else lookup()
    const denied = await settle()
    expect(expired).toHaveBeenCalledTimes(1)
    expect(draftLocked(denied)).toBe(true)
    expect(renderToStaticMarkup(denied)).toContain(labels.failures.sessionExpired)
    submitForm(denied); await settle()
    expect(submitEvaluation).not.toHaveBeenCalled()
    history.reject(new ApiProblemError('private', 401)); await settle()
    expect(expired).toHaveBeenCalledTimes(1)
  })

  it.each([
    { status: 403, code: 'permission_denied', key: 'forbidden' },
    { status: 404, code: 'run_not_found', key: 'notFound' },
  ] as const)('does not publish a late confirmation after history reports $status', async (denial) => {
    const history = deferred<EvaluationPage>()
    const confirmation = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(loadEvaluationPage).mockReturnValue(history.promise)
    vi.mocked(loadEvaluationSubmission).mockReturnValue(confirmation.promise)
    render(); commitHooks(); await settle(); lookup(); await settle()
    history.reject(new ApiProblemError('private', denial.status, denial.code)); await settle()
    expect(vi.mocked(loadEvaluationSubmission).mock.calls[0]![4]?.aborted).toBe(true)
    confirmation.resolve(evaluationReceipt()); const done = await settle()
    const html = renderToStaticMarkup(done)
    expect(html).toContain(labels.failures[denial.key])
    expect(html).not.toContain(labels.phase.confirmed)
    expect(html).not.toContain('data-evaluation-id=')
    expect(draftLocked(done)).toBe(true)
    expect(one(done, (item) => item.type === 'button' && text(item) === labels.confirm).props.disabled).toBe(true)
    expect(submitEvaluation).not.toHaveBeenCalled()
    expect(expired).not.toHaveBeenCalled()
  })

  it('retains an unknown original POST and disables resend after history denies access', async () => {
    const history = deferred<EvaluationPage>()
    vi.mocked(loadEvaluationPage).mockReturnValue(history.promise)
    vi.mocked(submitEvaluation).mockRejectedValue(new TypeError('synthetic network failure'))
    render(); commitHooks(); await settle(); submitForm(render()); await settle()
    const key = sent().submission_key
    history.reject(new ApiProblemError('private', 403)); const done = await settle()
    expect(field(done, labels.submissionKey).props.value).toBe(key)
    expect(one(done, (item) => item.type === 'button' && text(item) === labels.resend).props.disabled).toBe(true)
    expect(elements(done, (item) => item.type === 'button' && text(item) === labels.closeLookup)).toHaveLength(0)
    expect(submitEvaluation).toHaveBeenCalledTimes(1)
  })

  it('allows archived GET confirmation while the draft and every POST remain disabled', async () => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(loadEvaluationSubmission).mockReturnValue(response.promise)
    render(); commitHooks(); await settle(); lookup(); await settle()
    const archived = { writable: false, accessFailure: { key: 'projectArchived' as const } }
    render(archived); commitHooks()
    expect(vi.mocked(loadEvaluationSubmission).mock.calls[0]![4]?.aborted).toBe(false)
    response.resolve(evaluationReceipt())
    const done = await settle(archived)
    expect(renderToStaticMarkup(done)).toContain(labels.phase.confirmed)
    expect(renderToStaticMarkup(done)).toContain('data-evaluation-id=')
    expect(draftLocked(done)).toBe(true)
    expect(submitEvaluation).not.toHaveBeenCalled()
  })

  it('merges a late old history page without erasing the separately confirmed receipt', async () => {
    const history = deferred<EvaluationPage>()
    vi.mocked(loadEvaluationPage).mockReturnValue(history.promise)
    vi.mocked(submitEvaluation).mockImplementation(async (_project, _run, input) => evaluationReceipt(input))
    render(); commitHooks(); await settle(); submitForm(render()); await settle()
    const receiptId = evaluationReceipt().evaluation.evaluation_id
    expect(renderToStaticMarkup(render())).toContain(`data-evaluation-id="${receiptId}"`)
    const old = { ...evaluationReceipt().evaluation, evaluation_id: OTHER, created_at: '2026-09-10T00:00:00Z' }
    history.resolve(page([old])); const html = renderToStaticMarkup(await settle())
    expect(html.match(/data-evaluation-id=/g)).toHaveLength(2)
    expect(html).toContain(`data-evaluation-id="${receiptId}"`)
    expect(html).toContain(`data-evaluation-id="${OTHER}"`)
    expect(html.indexOf(`data-evaluation-id="${OTHER}"`)).toBeLessThan(html.indexOf(`data-evaluation-id="${receiptId}"`))
  })
})

describe('evaluation component owner wiring', () => {
  it('keeps the original request during same-scope detail loading and completes it without a new POST', async () => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(submitEvaluation).mockReturnValue(response.promise)
    render(); commitHooks(); await settle()
    change(render(), resultLabels.commentLabel, 'same owner')
    submitForm(render()); await settle()
    const original = structuredClone(sent())
    const loading = { writable: false }
    render(loading); commitHooks()
    expect(field(render(loading), labels.submissionKey).props.value).toBe(original.submission_key)
    expect(vi.mocked(submitEvaluation).mock.calls[0]![4]?.aborted).toBe(false)
    response.resolve(evaluationReceipt(original)); await settle(loading)
    const restored = await settle({ result: structuredClone(result) })
    expect(renderToStaticMarkup(restored)).toContain(labels.phase.confirmed)
    expect(field(restored, resultLabels.commentLabel).props.value).toBe('same owner')
    expect(submitEvaluation).toHaveBeenCalledTimes(1)
  })

  it('keeps the actual Result child key through loading/error but changes it for a different Result', () => {
    const detail: RunDetailRecord = { ...interactionDetail(), project_id: S.projectId, run_id: S.runId, result }
    /** Parent の state 保持だけを検証し、子 component の hooks は別 owner に留める。 */
    function owner(state: RunDetailState) {
      hookPhases.cursor = 0
      return RunEvaluations({ scope: S, state, csrfToken: 'synthetic-session', readOnly: false, onSessionExpired: expired })
    }
    const first = owner({ status: 'ready', detail })
    expect(first?.key).toBe(S.resultId)
    for (const state of [{ status: 'loading' }, { status: 'error', message: 'synthetic detail failure' }] as const) {
      const next = owner(state)
      expect(next?.key).toBe(first?.key)
      expect(next?.props.result).toBe(result)
      expect(next?.props.writable).toBe(false)
    }
    expect(owner({ status: 'ready', detail: structuredClone(detail) })?.key).toBe(first?.key)
    expect(owner({ status: 'ready', detail: { ...detail, result: { ...result, result_id: OTHER } } })?.key).toBe(OTHER)
    expect(owner({ status: 'ready', detail: { ...detail, result: null } })).toBeNull()
  })

  it('destroys the old pending owner on Result replacement and rejects its late session failure', async () => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(submitEvaluation).mockReturnValue(response.promise)
    render(); commitHooks(); await settle()
    change(render(), resultLabels.commentLabel, 'old result draft')
    submitForm(render()); await settle()
    // 実 JSX key の変更は前項で確認し、ここではその unmount 境界だけを明示する。
    unmountHooks()
    const changed = { scope: { ...S, resultId: OTHER }, result: { ...result, result_id: OTHER } }
    render(changed); commitHooks()
    response.reject(new ApiProblemError('private', 401)); const next = await settle(changed)
    expect(vi.mocked(submitEvaluation).mock.calls[0]![4]?.aborted).toBe(true)
    expect(expired).not.toHaveBeenCalled()
    expect(field(next, resultLabels.commentLabel).props.value).toBe('')
    expect(elements(next, (item) => item.props.className === 'evaluationSubmission')).toHaveLength(0)
  })

  it('assigns distinct sibling keys and binds the outer owner to actor, session, project and Run', () => {
    const props = { actorId: S.actorId, csrfToken: 'synthetic-session', projectId: S.projectId, runId: S.runId,
      state: { status: 'loading' as const } }
    /** RunResultPanel 自体の配線を読み、子 subtree を実行したことにはしない。 */
    function ownerKey(overrides: Partial<typeof props> = {}) {
      const tree = RunResultPanel({ ...props, ...overrides })
      return one(tree, (item) => item.type === RunEvaluations).key
    }
    const tree = RunResultPanel(props)
    const siblingKeys = elements(tree, (item) => item.key !== null).map((item) => item.key)
    expect(new Set(siblingKeys).size).toBe(siblingKeys.length)
    const initial = ownerKey()
    for (const changed of [{ actorId: OTHER }, { csrfToken: 'new-session' }, { projectId: OTHER }, { runId: OTHER }]) {
      expect(ownerKey(changed)).not.toBe(initial)
    }
    expect(ownerKey({ projectId: S.projectId.toUpperCase(), runId: S.runId.toUpperCase() })).toBe(initial)
  })
})
