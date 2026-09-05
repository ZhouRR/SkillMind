import type { PublishedTaskRecord, TaskReadinessRecord } from '../api'
import { useMessages } from '../i18n'
import { parseInputObject, sourceRequirements, type SourceRequirementChoice } from '../lib/taskDraft'
import { SchemaTaskInput } from './SchemaTaskInput'

/** 一つの資源要求の来源選択を、利用者が理解できる語彙で提示する。
 *
 * 技術 key(例: issue_provider)ではなく資源種別の友好名を主表示にし、原 key は
 * 追跡用に title へ退避する。必須で候補が一つだけなら選択させず「使用: 名称」を静的に示し、
 * 候補が無い必須要求は空 select ではなく設定導線を出す。複数候補のみ従来の select を残す。
 */
export function SourceRequirementField({ requirement, value, onChange }: {
  requirement: SourceRequirementChoice
  value: string
  onChange: (value: string) => void
}) {
  const messages = useMessages()
  const label = messages.workspace.resourceKind[requirement.kind] ?? requirement.key
  const soleOption = requirement.options[0]
  if (requirement.required && requirement.options.length === 1 && soleOption !== undefined) {
    return (
      <div className="sourceField" title={requirement.key}>
        <span className="sourceFieldLabel">{label}</span>
        <span className="sourceFieldStatic">{messages.workspace.willUseSource(soleOption.label)}</span>
      </div>
    )
  }
  if (requirement.required && requirement.options.length === 0) {
    return (
      <div className="sourceField" title={requirement.key}>
        <span className="sourceFieldLabel">{label}</span>
        <span className="hint">{messages.workspace.sourceNotConfigured}</span>
      </div>
    )
  }
  return (
    <label title={requirement.key}>{label}{requirement.required ? '' : messages.workspace.optionalSuffix}
      <select
        required={requirement.required}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{requirement.required ? messages.workspace.selectConfiguredResource : messages.workspace.notUsed}</option>
        {requirement.options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </label>
  )
}

/** 就緒度と各資源要求の不足理由・候補を逐項提示する。
 *
 * 「Gate 未通過」の一行では利用者が何を設定すればよいか判断できない。要求ごとに状態・理由・
 * 候補・選択指針を並べ、設定で解決できる不足と平台が対応していない要求を区別して示す。
 */
export function TaskReadinessPanel({ readiness }: { readiness: TaskReadinessRecord }) {
  const messages = useMessages()
  return (
    <div className="noteBlock">
      <h4>{messages.workspace.readinessTitle(messages.workspace.readinessLevels[readiness.level] ?? readiness.level)}</h4>
      {readiness.requirements.length === 0 && <p className="hint">{messages.workspace.noResourceNeeded}</p>}
      {readiness.requirements.length > 0 && (
        <ul className="noteList readinessList">
          {readiness.requirements.map((requirement) => (
            <li key={requirement.key}>
              {/* 見出しは資源種別の友好名を主にし、契約 key は追跡用の副表示へ落とす。
                  key と kind を並べても英語 2 連になるだけで、何を設定すべきか読めない。 */}
              <strong>
                {messages.workspace.resourceKind[requirement.kind] ?? requirement.kind}
                {' · '}
                {requirement.required ? messages.workspace.requiredLabel : messages.workspace.optionalLabel}
                <code className="mono">{requirement.key}</code>
              </strong>
              {/* backend の `reason` は status と 1:1 の英語固定文のため描画しない。
                  表示言語を守るため、同じ判断を status から語彙化する。 */}
              <span>
                {messages.workspace.requirementStatus[requirement.status] ?? requirement.status}
                {' — '}
                {messages.workspace.requirementReason[requirement.status] ?? ''}
                {requirement.candidates.length > 0
                  ? messages.workspace.candidatesLine(requirement.candidates.map((item) => item.label))
                  : ''}
              </span>
              {/* 選択指針は Skill 原文由来でレポート言語に従うため、平台文言とは行を分ける。 */}
              {requirement.selection_guidance && (
                <span className="readinessGuidance">{requirement.selection_guidance}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {readiness.level === 'GUIDANCE_ONLY' && (
        <p className="hint">{messages.workspace.guidanceOnlyHint}</p>
      )}
    </div>
  )
}

/** 一つの task を走らせるのに要る全項目(就緒度・来源選択・入力)をまとめた共通 fieldset。
 *
 * 即時実行(工作空间の弹窗)と時刻起動(任务中心の弹窗)が同じ実装を使う。二つの画面が別々に
 * 同じ form を持つと、片方だけ直された検証が生まれ、保存できるのに走らない設定を作れてしまう。
 */
export function TaskLaunchFields({ task, inputText, sourceProviders, onInputTextChange, onSourceChange }: {
  task: PublishedTaskRecord | null
  inputText: string
  sourceProviders: Record<string, string>
  onInputTextChange: (value: string) => void
  onSourceChange: (key: string, value: string) => void
}) {
  const messages = useMessages()
  if (!task) return null
  const requirements = sourceRequirements(task)
  return (
    <>
      {task.readiness && <TaskReadinessPanel readiness={task.readiness} />}
      {requirements.map((requirement) => (
        <SourceRequirementField
          key={requirement.key}
          requirement={requirement}
          value={sourceProviders[requirement.key] ?? ''}
          onChange={(value) => onSourceChange(requirement.key, value)}
        />
      ))}
      {requirements.length > 0 && <p className="hint">{messages.workspace.freezeHint}</p>}
      <SchemaTaskInput
        schema={task.input_schema}
        value={parseInputObject(inputText)}
        rawValue={inputText}
        onChange={(value) => onInputTextChange(JSON.stringify(value, null, 2))}
        onRawChange={onInputTextChange}
      />
      <p className="hint">{messages.workspace.inputValidatedHint}</p>
    </>
  )
}
