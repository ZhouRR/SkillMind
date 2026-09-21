import type { ReactNode } from 'react'

/** 画面と解釈詳細の tab を同じ keyboard 操作で切り替え、非表示の草稿は保持する。 */
export function SkillTabButton<T extends string>({ current, tab, onSelect, children }: {
  current: T
  tab: T
  onSelect: (tab: T) => void
  children: ReactNode
}) {
  return (
    <button
      aria-selected={current === tab}
      className="tab"
      onClick={() => onSelect(tab)}
      onKeyDown={(event) => {
        const buttons = Array.from(event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])
        const index = buttons.indexOf(event.currentTarget)
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
          : event.key === 'ArrowRight' ? (index + 1) % buttons.length
            : event.key === 'ArrowLeft' ? (index + buttons.length - 1) % buttons.length : null
        if (next === null) return
        event.preventDefault()
        buttons[next]?.focus()
        buttons[next]?.click()
      }}
      role="tab"
      tabIndex={current === tab ? 0 : -1}
      type="button"
    >
      {children}
    </button>
  )
}

