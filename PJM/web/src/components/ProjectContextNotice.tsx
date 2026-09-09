import type { ProjectAccess } from '../hooks/useProjectContext'
import { useMessages } from '../i18n'

/** リンク先の失効を一様に示し、未確認 context で操作を始めさせない。 */
export function ProjectContextNotice({ access, onRefresh }: { access: ProjectAccess; onRefresh: () => void }) {
  const messages = useMessages()
  if (access.status === 'ready') return access.project.status === 'ARCHIVED'
    ? <section className="panel" data-project-context="archived"><p role="status">{messages.app.projectArchived}</p></section>
    : null
  const text = access.status === 'loading' ? messages.elements.loadingProjects
    : access.status === 'empty' ? messages.elements.noAccessibleProjects
      : access.status === 'unavailable' ? messages.app.projectUnavailable : messages.app.projectReadFailed
  return <section className="panel" data-project-context={access.status} aria-busy={access.status === 'loading'}>
    <p role={access.status === 'error' || access.status === 'unavailable' ? 'alert' : 'status'}>{text}</p>
    {access.status !== 'loading' && <button className="secondaryButton" type="button" onClick={onRefresh}>{messages.runHistory.retry}</button>}
  </section>
}
