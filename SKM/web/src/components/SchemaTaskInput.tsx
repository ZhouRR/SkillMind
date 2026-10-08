import { Select } from './Select'
import { useRef, useState, type ChangeEvent } from 'react'

import { useMessages } from '../i18n'

/** Generated input Schema を標準 form へ投影するための最小 JSON Schema subset。 */
type FieldSchema = Record<string, unknown>

/** Generated Schema の top-level scalar field を form 化し、複雑型は JSON editor へ戻す。 */
export function SchemaTaskInput({
  schema,
  value,
  rawValue,
  onChange,
  onRawChange,
}: {
  schema: Record<string, unknown> | null
  value: Record<string, unknown> | null
  rawValue: string
  onChange: (value: Record<string, unknown>) => void
  onRawChange: (value: string) => void
}) {
  const messages = useMessages()
  const formDraft = useRef<Record<string, unknown>>(value ?? {})
  if (value !== null) formDraft.current = value
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const formValue = value ?? formDraft.current
  const editingInvalidRaw = value === null && Object.keys(drafts).length === 0
  const properties = isRecord(schema?.properties) ? schema.properties : null
  const required = new Set(
    Array.isArray(schema?.required)
      ? schema.required.filter((item): item is string => typeof item === 'string')
      : [],
  )
  if (schema?.type !== 'object' || properties === null) {
    return <RawJsonEditor value={rawValue} onChange={onRawChange} />
  }
  return (
    <div className="schemaInputForm">
      <fieldset className="schemaInputFields" disabled={editingInvalidRaw}>
      {Object.entries(properties).map(([key, rawSchema]) => {
        const field = isRecord(rawSchema) ? rawSchema : {}
        return (
          <SchemaField
            field={field}
            fieldKey={key}
            key={key}
            required={required.has(key)}
            value={formValue[key]}
            rawValue={drafts[key]}
            onRawChange={(raw) => {
              const next = { ...drafts, [key]: raw }
              setDrafts(next)
              onRawChange(serializeFieldDrafts(formValue, next, required))
            }}
            onChange={(next) => {
              const updated = { ...formValue, [key]: next }
              // 複雑 field が未完成でも、同じ form の scalar 編集を次の render に残す。
              formDraft.current = updated
              if (Object.keys(drafts).length) onRawChange(serializeFieldDrafts(updated, drafts, required))
              else onChange(updated)
            }}
          />
        )
      })}
      </fieldset>
      <details className="rawResult">
        <summary>{messages.taskInput.advancedJson}</summary>
        <RawJsonEditor value={rawValue} onChange={(raw) => { setDrafts({}); onRawChange(raw) }} />
      </details>
    </div>
  )
}

/** 一つの scalar field を type/enum に応じた 共有 form control へ変換する。 */
function SchemaField({ field, fieldKey, required, value, rawValue, onRawChange, onChange }: {
  field: FieldSchema
  fieldKey: string
  required: boolean
  value: unknown
  rawValue?: string
  onRawChange: (value: string) => void
  onChange: (value: unknown) => void
}) {
  const messages = useMessages()
  const label = typeof field.title === 'string' ? field.title : fieldKey
  const description = typeof field.description === 'string' ? field.description : null
  const enums = Array.isArray(field.enum) ? field.enum : null
  if (enums?.every((item) => ['string', 'number', 'boolean'].includes(typeof item))) {
    return (
      <label>{label}{required ? ' *' : ''}
        <Select required={required} value={scalarText(value)} onValueChange={(nextValue) => onChange(enumValue(enums, nextValue))}>
          {!required && <option value="">—</option>}
          {enums.map((item) => <option key={JSON.stringify(item)} value={scalarText(item)}>{scalarText(item)}</option>)}
        </Select>
        {description && <small>{description}</small>}
      </label>
    )
  }
  if (field.type === 'boolean') {
    return (
      <label>{label}{required ? ' *' : ''}
        <Select className="shortControl shortControlNarrow" density="compact" required={required} value={typeof value === 'boolean' ? String(value) : ''} onValueChange={(nextValue) => onChange(nextValue === '' ? undefined : nextValue === 'true')}>
          {!required && <option value="">—</option>}
          <option value="true">true</option><option value="false">false</option>
        </Select>
        {description && <small>{description}</small>}
      </label>
    )
  }
  if (field.type === 'integer' || field.type === 'number') {
    return (
      <label>{label}{required ? ' *' : ''}
        <input
          required={required}
          step={field.type === 'integer' ? 1 : 'any'}
          type="number"
          value={typeof value === 'number' ? value : ''}
          onChange={(event) => onChange(numberValue(event, field.type === 'integer'))}
        />
        {description && <small>{description}</small>}
      </label>
    )
  }
  if (field.type === 'string') {
    return (
      <label>{label}{required ? ' *' : ''}
        <input required={required} type="text" value={typeof value === 'string' ? value : ''} onChange={(event) => onChange(event.target.value)} />
        {description && <small>{description}</small>}
      </label>
    )
  }
  const raw = rawValue ?? (value === undefined ? '' : JSON.stringify(value, null, 2))
  const invalid = raw.trim() === '' ? required : !validJson(raw)
  return (
    <label>{label}{required ? ' *' : ''}{messages.taskInput.jsonSuffix}
      <textarea
        rows={4}
        spellCheck={false}
        required={required}
        aria-invalid={invalid || undefined}
        value={raw}
        onChange={(event) => onRawChange(event.target.value)}
      />
      {invalid && <small className="error" role="status">{messages.uiAuditWorkspace.invalidJson}</small>}
      {description && <small>{description}</small>}
    </label>
  )
}

/** 未完成の field JSON も原文のまま合成し、送信境界で不正入力を確実に止める。 */
function serializeFieldDrafts(value: Record<string, unknown>, drafts: Record<string, string>, required: ReadonlySet<string>): string {
  const keys = new Set([...Object.keys(value), ...Object.keys(drafts)])
  const combined = `{\n${[...keys].filter((key) => key in drafts ? drafts[key]!.trim() !== '' : value[key] !== undefined)
    .map((key) => `  ${JSON.stringify(key)}: ${drafts[key] ?? JSON.stringify(value[key])}`).join(',\n')}\n}`
  // field 内へ兄弟 key に見える文字列を入力しても、単一 JSON 値として不正なら送信不可とする。
  const invalid = Object.entries(drafts).some(([key, raw]) => raw.trim() === '' ? required.has(key) : !validJson(raw))
  return invalid ? `${combined},` : combined
}

/** 途中の構文不正を例外ではなく編集状態として扱う。 */
function validJson(raw: string): boolean {
  try { JSON.parse(raw); return true } catch { return false }
}

/** Schema form が扱えない場合にも入力可能性を失わない raw JSON editor。 */
function RawJsonEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const messages = useMessages()
  const invalid = !validJson(value) || !isRecord(JSON.parse(value))
  return <label>{messages.taskInput.rawLabel}<textarea className="jsonInput" aria-invalid={invalid || undefined} rows={6} spellCheck={false} value={value} onChange={(event) => onChange(event.target.value)} />{invalid && <small className="error" role="status">{messages.uiAuditWorkspace.invalidJson}</small>}</label>
}

/** Unknown JSON が object か判定する。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Scalar を select の安定した文字列表現へ変換する。 */
function scalarText(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value) ?? ''
}

/** Select value を元の enum 型へ戻す。 */
function enumValue(values: unknown[], selected: string): unknown {
  return values.find((item) => scalarText(item) === selected)
}

/** Number input の空値と integer 丸めを JSON value へ変換する。 */
function numberValue(event: ChangeEvent<HTMLInputElement>, integer: boolean): number | undefined {
  if (event.target.value === '') return undefined
  const value = event.target.valueAsNumber
  return integer ? Math.trunc(value) : value
}
