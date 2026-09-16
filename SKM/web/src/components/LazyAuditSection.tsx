import { useState, type ReactNode } from 'react'

/** 備査内容は開いた時だけ評価する。未決承認や UNKNOWN の警告は呼出し側に常時残す。 */
export function LazyAuditSection({ title, count, children }: {
  title: string
  count?: number
  children: () => ReactNode
}) {
  const [expanded, setExpanded] = useState(false)
  return (
    <details className="resultCollapse" onToggle={(event) => {
      // 内側の details の toggle で外側の表示状態を上書きしない。
      if (event.target === event.currentTarget) setExpanded(event.currentTarget.open)
    }}>
      <summary>
        <span className="resultCollapseTitle">{title}</span>
        {count !== undefined && <span className="eventCount">{count}</span>}
      </summary>
      {expanded && <div className="resultCollapseBody">{children()}</div>}
    </details>
  )
}
