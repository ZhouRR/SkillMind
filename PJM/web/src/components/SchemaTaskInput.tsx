import type { ChangeEvent } from 'react'

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
  const properties = isRecord(schema?.properties) ? schema.properties : null
  const required = new Set(
    Array.isArray(schema?.required)
      ? schema.required.filter((item): item is string => typeof item === 'string')
      : [],
  )
  if (schema?.type !== 'object' || properties === null || value === null) {
    return <RawJsonEditor value={rawValue} onChange={onRawChange} />
  }
  return (
    <div className="schemaInputForm">
      {Object.entries(properties).map(([key, rawSchema]) => {
        const field = isRecord(rawSchema) ? rawSchema : {}
        return (
          <SchemaField
            field={field}
            fieldKey={key}
            key={key}
            required={required.has(key)}
            value={value[key]}
            onChange={(next) => onChange({ ...value, [key]: next })}
          />
        )
      })}
      <details className="rawResult">
        <summary>{messages.taskInput.advancedJson}</summary>
        <RawJsonEditor value={rawValue} onChange={onRawChange} />
      </details>
    </div>
  )
}

/** 一つの scalar field を type/enum に応じた native control へ変換する。 */
function SchemaField({ field, fieldKey, required, value, onChange }: {
  field: FieldSchema
  fieldKey: string
  required: boolean
  value: unknown
  onChange: (value: unknown) => void
}) {
  const messages = useMessages()
  const label = typeof field.title === 'string' ? field.title : fieldKey
  const description = typeof field.description === 'string' ? field.description : null
  const enums = Array.isArray(field.enum) ? field.enum : null
  if (enums?.every((item) => ['string', 'number', 'boolean'].includes(typeof item))) {
    return (
      <label>{label}{required ? ' *' : ''}
        <select required={required} value={scalarText(value)} onChange={(event) => onChange(enumValue(enums, event.target.value))}>
          {!required && <option value="">—</option>}
          {enums.map((item) => <option key={JSON.stringify(item)} value={scalarText(item)}>{scalarText(item)}</option>)}
        </select>
        {description && <small>{description}</small>}
      </label>
    )
  }
  if (field.type === 'boolean') {
    return (
      <label>{label}{required ? ' *' : ''}
        <select required={required} value={typeof value === 'boolean' ? String(value) : ''} onChange={(event) => onChange(event.target.value === '' ? undefined : event.target.value === 'true')}>
          {!required && <option value="">—</option>}
          <option value="true">true</option><option value="false">false</option>
        </select>
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
  return (
    <label>{label}{required ? ' *' : ''}{messages.taskInput.jsonSuffix}
      <textarea
        rows={4}
        spellCheck={false}
        value={value === undefined ? '' : JSON.stringify(value, null, 2)}
        onChange={(event) => {
          try { onChange(JSON.parse(event.target.value) as unknown) } catch { /* 完成した JSON まで保持する。 */ }
        }}
      />
      {description && <small>{description}</small>}
    </label>
  )
}

/** Schema form が扱えない場合にも入力可能性を失わない raw JSON editor。 */
function RawJsonEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const messages = useMessages()
  return <label>{messages.taskInput.rawLabel}<textarea className="jsonInput" rows={6} spellCheck={false} value={value} onChange={(event) => onChange(event.target.value)} /></label>
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
