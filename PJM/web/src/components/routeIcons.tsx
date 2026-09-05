import type { ReactNode } from 'react'

import type { AppRoute } from '../lib/routing'

/** 各画面を識別する 16px の stroke icon。sidebar 導航と概览の模块 card が共有し、外部 icon library に依存しない。 */
export const ROUTE_ICONS: Record<AppRoute, ReactNode> = {
  home: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M2.5 6.5 8 2l5.5 4.5V13a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1Z" />
      <path d="M6 14v-4h4v4" />
    </svg>
  ),
  skills: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 1.8 14 5v6L8 14.2 2 11V5Z" />
      <path d="M2 5l6 3 6-3M8 8v6" />
    </svg>
  ),
  projects: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M1.8 4.2a1 1 0 0 1 1-1h3.4l1.6 1.8h5.4a1 1 0 0 1 1 1v6.2a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1Z" />
    </svg>
  ),
  documents: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M4 1.8h5l3 3V14a.8.8 0 0 1-.8.8H4a.8.8 0 0 1-.8-.8V2.6A.8.8 0 0 1 4 1.8Z" />
      <path d="M9 1.8V5h3M5.6 8.4h4.8M5.6 11h4.8" />
    </svg>
  ),
  resources: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M2.2 4.6h11.6M2.2 8h11.6M2.2 11.4h11.6" />
      <circle cx="5.4" cy="4.6" r="1.3" />
      <circle cx="10.6" cy="8" r="1.3" />
      <circle cx="6.6" cy="11.4" r="1.3" />
    </svg>
  ),
  // 任务中心は「予定表 + チェック」。工作空间(端末)と役割が違うことを形で示す。
  tasks: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <rect height="11.2" rx="1" width="12.4" x="1.8" y="2.6" />
      <path d="M1.8 6h12.4M5 1.8v2.4M11 1.8v2.4M5.6 9.6l1.5 1.6 3.3-3.4" />
    </svg>
  ),
  workspace: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <rect height="10.4" rx="1" width="12.4" x="1.8" y="2.8" />
      <path d="m4.6 6 2 2-2 2M8.4 10h3" />
    </svg>
  ),
  history: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M2.2 4.2h11.6v9H2.2z" />
      <path d="M5 2.2v4M11 2.2v4M4.5 9h2M9.5 9h2M4.5 11.5h2" />
    </svg>
  ),
}
