import type { KeyboardEvent, ReactNode } from 'react'

/** 同じ tablist 内の有効な tab を循環し、選択と keyboard focus を同期する。 */
export function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>): void {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const tabs = [...(event.currentTarget.closest('[role="tablist"]')?.querySelectorAll<HTMLButtonElement>('[role="tab"]:not(:disabled)') ?? [])]
  const index = tabs.indexOf(event.currentTarget)
  if (index < 0 || tabs.length === 0) return
  event.preventDefault()
  const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
    : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
  tabs[nextIndex]?.focus()
  tabs[nextIndex]?.click()
}

/** tab/panel の関係と roving tabindex を一箇所で維持する。panel の寿命は呼出側が持つ。 */
export function TabButton({ id, panelId, selected, onSelect, children }: {
  id: string; panelId: string; selected: boolean; onSelect: () => void; children: ReactNode
}) {
  return <button id={id} className="tab" role="tab" type="button" aria-selected={selected}
    aria-controls={panelId} tabIndex={selected ? 0 : -1} onKeyDown={handleTabKeyDown} onClick={onSelect}>{children}</button>
}
