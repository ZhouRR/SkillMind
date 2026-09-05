import { DocumentManagerPanel } from '../components/DocumentManagerPanel'
import { PageHeader } from '../components/PageElements'
import { useMessages } from '../i18n'

/** 現在 Project の文書を管理する専用画面。Project 切替は sidebar が持つ。 */
export function DocumentsPage({ projectId, csrfToken }: {
  projectId: string
  csrfToken: string
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
        <DocumentManagerPanel csrfToken={csrfToken} projectId={projectId} />
      </section>
    </>
  )
}
