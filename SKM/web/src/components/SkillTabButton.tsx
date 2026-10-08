import { handleTabKeyDown } from './TabButton'
import type { ReactNode } from 'react'

/** 画面と解釈詳細の tab を同じ keyboard 操作で切り替え、非表示の草稿は保持する。 */
export function SkillTabButton<T extends string>({ idPrefix, current, tab, onSelect, children }: {
  idPrefix: string
  current: T
  tab: T
  onSelect: (tab: T) => void
  children: ReactNode
}) {
  return (
    <button
      id={`${idPrefix}-tab-${tab}`}
      aria-controls={`${idPrefix}-panel-${tab}`}
      aria-selected={current === tab}
      className="tab"
      onClick={() => onSelect(tab)}
      onKeyDown={handleTabKeyDown}
      role="tab"
      tabIndex={current === tab ? 0 : -1}
      type="button"
    >
      {children}
    </button>
  )
}

