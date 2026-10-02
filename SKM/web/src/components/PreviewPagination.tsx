import { useMessages } from '../i18n'

/** 本文 token を複製せず、現在頁と総頁数だけで操作を構成する。 */
export function PreviewPagination({ index, count, select }: {
  index: number; count: number; select: (index: number) => void
}) {
  const messages = useMessages()
  if (count <= 1) return null
  return <nav className="markdownPreviewPagination" aria-label={messages.documentsPanel.previewPages}>
    <button type="button" className="secondaryButton compactButton" disabled={index === 0}
      onClick={() => select(index - 1)}>{messages.runHistory.previous}</button>
    <select aria-label={messages.documentsPanel.previewPages} value={index}
      onChange={(event) => select(Number(event.target.value))}>
      {Array.from({ length: count }, (_, ordinal) => <option key={ordinal} value={ordinal}>
        {messages.documentsPanel.previewPage(ordinal + 1, count)}
      </option>)}
    </select>
    <button type="button" className="secondaryButton compactButton" disabled={index === count - 1}
      onClick={() => select(index + 1)}>{messages.runHistory.next}</button>
  </nav>
}
