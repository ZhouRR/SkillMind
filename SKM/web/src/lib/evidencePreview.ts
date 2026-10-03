import type { EvidenceDetail } from '../api'

/** 原文は別途保持し、サーバーが原記録から投影した有界の表だけ preview に使う。 */
export function evidencePreviewSource(evidence: EvidenceDetail): string | null {
  const value = evidence.metadata.excerpt_preview
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const preview = value as Record<string, unknown>
  return preview.version === 'markdown-table/v1' && typeof preview.source === 'string'
    && preview.source.length > 0 && preview.source.length <= 4096
    && positiveLine(preview.header_line_start) && positiveLine(preview.line_start)
    && positiveLine(preview.line_end) && preview.header_line_start < preview.line_start
    && preview.line_start <= preview.line_end ? preview.source : null
}

/** 不正な範囲を context として表示せず、通常の抜粋へ戻す。 */
function positiveLine(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}
