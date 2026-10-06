import { Select as SelectPrimitive } from '@base-ui/react/select'
import { Children, Fragment, isValidElement, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { modalTabStops } from '../lib/focus'
import type { ButtonHTMLAttributes, OptgroupHTMLAttributes, OptionHTMLAttributes, ReactNode } from 'react'

/** 単一選択の公開契約。値変更は DOM event を偽造せず、文字列の値を直接返す。 */
export interface SelectProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'value' | 'defaultValue' | 'onChange'> {
  children: ReactNode
  value?: string | number
  defaultValue?: string | number
  onValueChange?: (value: string) => void
  required?: boolean
  readOnly?: boolean
  autoComplete?: string
}

/** Native option の表示・送信値・無効状態を popup へ渡す中間表現。 */
interface SelectOption {
  kind: 'option'
  key: string
  value: string
  label: string
  disabled: boolean
  hidden: boolean
}

/** optgroup の見出しと無効状態を子 option に維持する。 */
interface SelectGroup {
  kind: 'group'
  key: string
  label: string
  options: SelectEntry[]
}

type SelectEntry = SelectOption | SelectGroup

/** option 内の配列・fragment を、native option と同じ文字列にまとめる。 */
function optionText(children: ReactNode): string {
  return Children.toArray(children).map((child) => {
    if (typeof child === 'string' || typeof child === 'number') return String(child)
    return isValidElement<{ children?: ReactNode }>(child) ? optionText(child.props.children) : ''
  }).join('')
}

/** 既存の option/optgroup 宣言を解析する。独自 component の暗黙実行は行わない。 */
function parseOptions(children: ReactNode, parentKey = '', groupDisabled = false): SelectEntry[] {
  return Children.toArray(children).flatMap((child, index): SelectEntry[] => {
    if (!isValidElement(child)) return []
    const key = `${parentKey}/${child.key ?? index}`
    if (child.type === Fragment && isValidElement<{ children?: ReactNode }>(child)) {
      return parseOptions(child.props.children, key, groupDisabled)
    }
    if (child.type === 'optgroup' && isValidElement<OptgroupHTMLAttributes<HTMLOptGroupElement>>(child)) {
      return [{
        kind: 'group', key, label: child.props.label ?? '',
        options: parseOptions(child.props.children, key, groupDisabled || Boolean(child.props.disabled)),
      }]
    }
    if (child.type === 'option' && isValidElement<OptionHTMLAttributes<HTMLOptionElement>>(child)) {
      const text = optionText(child.props.children)
      return [{
        kind: 'option', key, value: String(child.props.value ?? text),
        label: child.props.label ?? text,
        disabled: groupDisabled || Boolean(child.props.disabled),
        hidden: Boolean(child.props.hidden),
      }]
    }
    return []
  })
}

/** Group の階層と無関係に表示名解決と既定値選択へ使う option 一覧を作る。 */
function flattenOptions(entries: SelectEntry[]): SelectOption[] {
  return entries.flatMap((entry) => entry.kind === 'option' ? [entry] : flattenOptions(entry.options))
}

/** 選択値を色だけで示さず、checkmark と aria-selected も同時に提供する。 */
function SelectEntries({ entries }: { entries: SelectEntry[] }) {
  return entries.map((entry) => entry.kind === 'group' ? (
    <SelectPrimitive.Group key={entry.key} className="selectGroup">
      <SelectPrimitive.GroupLabel className="selectGroupLabel">{entry.label}</SelectPrimitive.GroupLabel>
      <SelectEntries entries={entry.options} />
    </SelectPrimitive.Group>
  ) : !entry.hidden && (
    <SelectPrimitive.Item key={entry.key} value={entry.value} label={entry.label}
      disabled={entry.disabled} data-value={entry.value} className="selectItem">
      <SelectPrimitive.ItemText className="selectItemText">{entry.label}</SelectPrimitive.ItemText>
      <SelectPrimitive.ItemIndicator className="selectItemIndicator">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3 8 3.2 3.2L13 4.5" /></svg>
      </SelectPrimitive.ItemIndicator>
    </SelectPrimitive.Item>
  ))
}

/** Browser 固有 picker を使わない共有 select。焦点・typeahead・dismissal は Base UI に委ねる。
 *  hidden input で required/name/form を維持し、native label は実 button に関連付く。 */
export function Select({ children, value, defaultValue, onValueChange, disabled, required, readOnly,
  name, form, autoComplete, id, className, ...triggerProps }: SelectProps) {
  const generatedId = useId()
  const controlId = id ?? `select-${generatedId}`
  const entries = useMemo(() => parseOptions(children), [children])
  const options = useMemo(() => flattenOptions(entries), [entries])
  const initialValue = useRef(String(defaultValue ?? value ?? options.find((option) => !option.disabled)?.value ?? ''))
  const [uncontrolledValue, setUncontrolledValue] = useState(initialValue.current)
  const selectedValue = value === undefined ? uncontrolledValue : String(value)
  const [portalContainer, setPortalContainer] = useState<HTMLElement | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const controlledRef = useRef(value !== undefined)
  const keyboardInteraction = useRef(false)
  const tabFocusTarget = useRef<HTMLElement | null>(null)
  const resetValueRef = useRef(initialValue.current)
  const [invalid, setInvalid] = useState(false)
  const [fieldsetDisabled, setFieldsetDisabled] = useState(false)
  const [open, setOpen] = useState(false)
  const effectiveDisabled = disabled || fieldsetDisabled

  useEffect(() => {
    controlledRef.current = value !== undefined
    resetValueRef.current = defaultValue === undefined ? initialValue.current : String(defaultValue)
  }, [defaultValue, value])

  const setTrigger = useCallback((element: HTMLButtonElement | null) => {
    triggerRef.current = element
    // popup を scroll する modalBody の外へ出し、aria-modal の内側には残す。
    setPortalContainer(element?.closest<HTMLElement>('.modalDialog') ?? null)
  }, [])

  useEffect(() => {
    const trigger = triggerRef.current
    if (!trigger) return
    // Portal は fieldset の外へ出るため、native な継承無効状態を Root にも同期する。
    const syncDisabled = () => {
      let inherited = false
      for (let ancestor = trigger.parentElement; ancestor; ancestor = ancestor.parentElement) {
        if (!(ancestor instanceof HTMLFieldSetElement) || !ancestor.disabled) continue
        const firstLegend = Array.from(ancestor.children).find((child) => child.tagName === 'LEGEND')
        if (!firstLegend?.contains(trigger)) inherited = true
      }
      setFieldsetDisabled(inherited)
      if (disabled || inherited) setOpen(false)
    }
    const observer = new MutationObserver(syncDisabled)
    for (let ancestor = trigger.parentElement; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor.tagName === 'FIELDSET') observer.observe(ancestor, { attributes: true, attributeFilter: ['disabled'] })
    }
    syncDisabled()
    return () => observer.disconnect()
  }, [disabled])

  useEffect(() => {
    const input = inputRef.current
    const ownerForm = input?.form
    let active = true
    const handleReset = (event: Event) => {
      // reset は取り消せる。form の onReset が終わってから状態を同期する。
      queueMicrotask(() => {
        if (!active || event.defaultPrevented) return
        // controlled form は親の onReset が所有する。native reset 同様、change 通知は出さない。
        if (!controlledRef.current) setUncontrolledValue(resetValueRef.current)
        setInvalid(false)
      })
    }
    const handleInvalid = () => {
      setInvalid(true)
      triggerRef.current?.focus()
    }
    ownerForm?.addEventListener('reset', handleReset)
    input?.addEventListener('invalid', handleInvalid)
    return () => {
      active = false
      ownerForm?.removeEventListener('reset', handleReset)
      input?.removeEventListener('invalid', handleInvalid)
    }
  }, [form])

  return (
    <SelectPrimitive.Root<string> id={controlId} value={selectedValue} items={options}
      disabled={effectiveDisabled} open={open} required={required} readOnly={readOnly} name={name} form={form}
      autoComplete={autoComplete} inputRef={inputRef} modal={false}
      onValueChange={(nextValue, details) => {
        // 候補再取得時の初期値/null fallback はユーザー選択ではない。typeahead の同期通知だけ通す。
        if (nextValue === null || triggerRef.current?.matches(':disabled')
          || (details.reason === 'none' && details.event.type === 'base-ui' && !keyboardInteraction.current)) {
          details.cancel()
          return
        }
        const normalizedValue = nextValue
        setUncontrolledValue(normalizedValue)
        setInvalid(false)
        onValueChange?.(normalizedValue)
      }}
      onOpenChange={(nextOpen, details) => {
        if (nextOpen && triggerRef.current?.matches(':disabled')) { details.cancel(); return }
        if (nextOpen) tabFocusTarget.current = null
        setOpen(nextOpen)
        // 同じ Escape が親 modal の window listener に届いて二重に閉じないようにする。
        if (details.reason === 'escape-key') details.event.stopPropagation()
      }}>
      <SelectPrimitive.Trigger {...triggerProps} ref={setTrigger} id={controlId}
        className={`selectTrigger${className ? ` ${className}` : ''}`} data-value={selectedValue} data-field-name={name}
        onKeyDownCapture={(event) => {
          // Base UI の閉じた trigger の typeahead は native event を通知に含めない。
          keyboardInteraction.current = true
          queueMicrotask(() => { keyboardInteraction.current = false })
          triggerProps.onKeyDownCapture?.(event)
        }}
        aria-invalid={triggerProps['aria-invalid'] ?? (invalid && !selectedValue ? true : undefined)}>
        <SelectPrimitive.Value className="selectValue" />
        <SelectPrimitive.Icon className="selectIcon">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal container={portalContainer ?? undefined}>
        <SelectPrimitive.Positioner className="selectPositioner" positionMethod="fixed"
          alignItemWithTrigger={false} sideOffset={6} collisionPadding={10} align="start">
          <SelectPrimitive.Popup className="selectPopup" data-select-popup="" aria-labelledby={controlId}
            finalFocus={() => tabFocusTarget.current ?? true}
            onKeyDownCapture={(event) => {
              if (event.key !== 'Tab' || !portalContainer || !triggerRef.current) return
              // 外側 modal の Tab 順を保ち、portal 末尾から dialog 外へ漏れるのを防ぐ。
              const controls = modalTabStops(portalContainer)
              const index = controls.indexOf(triggerRef.current)
              if (index < 0 || controls.length === 0) return
              tabFocusTarget.current = controls[(index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length] ?? null
              event.preventDefault()
              event.stopPropagation()
              setOpen(false)
            }}>
            <SelectEntries entries={entries} />
          </SelectPrimitive.Popup>
        </SelectPrimitive.Positioner>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  )
}
