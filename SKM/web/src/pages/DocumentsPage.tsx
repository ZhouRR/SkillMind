import { DocumentManagerPanel } from '../components/DocumentManagerPanel'
import { PageHeader } from '../components/PageElements'
import { useMessages } from '../i18n'
import type { SessionEnded } from '../hooks/useResourceRequest'

/** 現在 Project の文書を管理する専用画面。Project 切替は sidebar が持つ。 */
export function DocumentsPage({ projectId, csrfToken, actorId, readOnly, onSessionEnded }: {
  projectId: string
  csrfToken: string
  actorId: string
  readOnly: boolean
  onSessionEnded: SessionEnded
}) {
  const messages = useMessages()
  return (
    <>
      <PageHeader
        title={messages.routes.documents.label}
        description={messages.documentsPage.description}
        aside={<span className="scopeBadge">{messages.documentsPage.scopeBadge}</span>}
      />
      <section className="documentsPage" aria-label={messages.documentsPage.pageAria}>
        <DocumentManagerPanel csrfToken={csrfToken} projectId={projectId} actorId={actorId}
          readOnly={readOnly} onSessionEnded={onSessionEnded} />
      </section>
    </>
  )
}
