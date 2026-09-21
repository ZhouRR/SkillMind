import { useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'

import '../styles/action-menu.css'

/** 呼出元が確定した操作だけを表示する。認可・確認・通信は元の handler が所有する。 */
export interface ActionMenuItem {
  id: string
  label: string
  onSelect?: () => void
  href?: string
  download?: string
  disabled?: boolean
  danger?: boolean
  separatorBefore?: boolean
}

/** 行の常設入口と非 modal メニュー。Portal により directory/list の clipping を避ける。 */
export function ActionMenu({ id, label, items, disabled = false, ownerKey = label }: {
  /** 一覧再取得で行が置換された後も同じ対象へ焦点を戻すための安定 ID。 */
  id?: string
  label: string
  items: readonly ActionMenuItem[]
  disabled?: boolean
  /** 同名対象でも識別できる元 ID。行を対象 ID で key 付けする場合は省略可。 */
  ownerKey?: string
}) {
  const menuId = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const mounted = useRef(false)
  const openRef = useRef(false)
  const initialFocus = useRef<'first' | 'last'>('first')
  const current = useRef({ ownerKey, items, disabled })
  current.current = { ownerKey, items, disabled }
  const [openOwner, setOpenOwner] = useState<string | null>(null)
  const unavailable = disabled || items.length === 0
  const open = openOwner === ownerKey && !unavailable
  openRef.current = open

  useLayoutEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; openRef.current = false }
  }, [])

  useLayoutEffect(() => { setOpenOwner(null) }, [ownerKey, unavailable])

  /** 後続 dialog が focus を取るより先に元 button へ戻す。取消/rollback は行わない。 */
  function close(restoreFocus = false): void {
    openRef.current = false
    setOpenOwner(null)
    if (restoreFocus) trigger.current?.focus()
  }

  function focusItems(): HTMLElement[] {
    return Array.from(panel.current?.querySelectorAll<HTMLElement>(
      '[role="menuitem"]:not(:disabled):not([aria-disabled="true"])',
    ) ?? [])
  }

  useLayoutEffect(() => {
    if (!open) return
    const menu = panel.current
    const button = trigger.current
    if (!menu || !button) return

    /** 表示領域内へ収め、短い画面ではメニュー自身だけを scroll させる。 */
    function position(): void {
      if (!menu || !button) return
      const margin = 8
      const gap = 4
      const width = Math.min(272, Math.max(1, window.innerWidth - margin * 2))
      const maxHeight = Math.max(1, window.innerHeight - margin * 2)
      menu.style.width = `${width}px`
      menu.style.maxHeight = `${maxHeight}px`
      const bounds = button.getBoundingClientRect()
      const height = Math.min(menu.scrollHeight + 2, maxHeight)
      const below = bounds.bottom + gap
      const preferredTop = below + height <= window.innerHeight - margin
        ? below : bounds.top - gap - height
      menu.style.left = `${Math.max(margin, Math.min(bounds.right - width, window.innerWidth - width - margin))}px`
      menu.style.top = `${Math.max(margin, Math.min(preferredTop, window.innerHeight - height - margin))}px`
    }
    position()
    const candidates = focusItems()
    ;(initialFocus.current === 'last' ? candidates.at(-1) : candidates[0])?.focus()
    if (candidates.length === 0) menu.focus()

    function outside(event: Event): void {
      const target = event.target
      if (target instanceof Node && !menu?.contains(target) && !button?.contains(target)) close()
    }
    window.addEventListener('resize', position)
    window.addEventListener('scroll', position, true)
    document.addEventListener('pointerdown', outside, true)
    document.addEventListener('focusin', outside)
    return () => {
      window.removeEventListener('resize', position)
      window.removeEventListener('scroll', position, true)
      document.removeEventListener('pointerdown', outside, true)
      document.removeEventListener('focusin', outside)
    }
  }, [open, ownerKey])

  function show(focus: 'first' | 'last' = 'first'): void {
    if (unavailable || !mounted.current) return
    initialFocus.current = focus
    setOpenOwner(ownerKey)
  }

  /** Disabled/旧対象の callback は実行しない。リンクは元 href/download の native 動作を使う。 */
  function select(itemId: string): boolean {
    if (!mounted.current || !openRef.current || current.current.ownerKey !== ownerKey || current.current.disabled) return false
    const item = current.current.items.find((candidate) => candidate.id === itemId)
    if (!item || item.disabled) return false
    close(true)
    item.onSelect?.()
    return true
  }

  function navigate(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close(true)
      return
    }
    if (event.key === 'Tab') {
      // Portal 末尾からではなく元 button の位置から通常の Tab 順序へ戻す。
      close(true)
      return
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    event.stopPropagation()
    const candidates = focusItems()
    const index = candidates.indexOf(document.activeElement as HTMLElement)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? candidates.length - 1
      : (index + (event.key === 'ArrowDown' ? 1 : -1) + candidates.length) % candidates.length
    candidates[next]?.focus()
  }

  if (items.length === 0) return null
  return <>
    <button id={id} className="actionMenuTrigger" type="button" aria-label={label} title={label}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
      disabled={unavailable} ref={trigger}
      onClick={(event) => { event.preventDefault(); event.stopPropagation(); if (openRef.current) close(true); else show() }}
      onKeyDown={(event) => {
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
        event.preventDefault(); event.stopPropagation()
        show(event.key === 'ArrowUp' ? 'last' : 'first')
      }}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg>
    </button>
    {open && createPortal(<div className="actionMenuPanel" id={menuId} role="menu" aria-label={label}
      ref={panel} tabIndex={-1} onKeyDown={navigate} onClick={(event) => event.stopPropagation()}>
      {items.map((item) => <div className="actionMenuEntry" key={item.id}>
        {item.separatorBefore && <div className="actionMenuSeparator" role="separator" />}
        {item.href !== undefined ? <a className={`actionMenuItem${item.danger ? ' actionMenuDanger' : ''}`}
          role="menuitem" tabIndex={-1} aria-disabled={item.disabled || undefined}
          href={item.disabled ? undefined : item.href} download={item.download}
          onClick={(event) => { if (!select(item.id)) event.preventDefault() }}
          onKeyDown={(event) => {
            if (event.key === ' ') { event.preventDefault(); event.currentTarget.click() }
          }}>{item.label}</a>
          : <button className={`actionMenuItem${item.danger ? ' actionMenuDanger' : ''}`} type="button"
            role="menuitem" tabIndex={-1} disabled={item.disabled} onClick={() => select(item.id)}>{item.label}</button>}
      </div>)}
    </div>, document.body)}
  </>
}
