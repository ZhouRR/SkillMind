import { useEffect, useState } from 'react'

import { LanguageProvider } from '../../src/i18n'
import type { UiLanguage } from '../../src/lib/i18n/messages'
import { WorkspacePage } from '../../src/pages/WorkspacePage'
import { TasksPage } from '../../src/pages/TasksPage'

/** Browser fixture の認証 context。API は検証 runner が全面的に mock する。 */
interface TestContext {
  actorId: string
  projectId: string
  csrfToken: string
  language: UiLanguage
  screen: 'workspace' | 'tasks'
  initialRunId: string | null
}

declare global {
  interface Window {
    /** 認証/Project/言語の切替を再現する test 専用入口。production entry から import しない。 */
    updateSubmissionTestContext?: (context: Partial<TestContext>) => void
  }
}

/** App と同じ actor/Project の remount 境界で、実 Workspace と Hook を動かす。 */
export function SubmissionHarness() {
  const [context, setContext] = useState<TestContext>({
    actorId: '00000000-0000-4000-8000-000000000001',
    projectId: '00000000-0000-4000-8000-000000000020',
    csrfToken: 'c'.repeat(32), language: 'zh',
    screen: 'workspace', initialRunId: new URLSearchParams(location.search).get('run'),
  })
  useEffect(() => {
    window.updateSubmissionTestContext = (next) => setContext((current) => ({ ...current, ...next }))
    return () => { delete window.updateSubmissionTestContext }
  }, [])
  return (
    <LanguageProvider language={context.language}>
      {context.screen === 'tasks' ? <TasksPage
        key={`${context.actorId}:${context.projectId}`}
        csrfToken={context.csrfToken}
        projectId={context.projectId}
        moduleId=""
      /> : <WorkspacePage
        key={`${context.actorId}:${context.projectId}`}
        actorId={context.actorId}
        csrfToken={context.csrfToken}
        projectId={context.projectId}
        moduleId=""
        initialRunId={context.initialRunId}
        onSessionExpired={() => window.dispatchEvent(new Event('interaction-session-expired'))}
      />}
    </LanguageProvider>
  )
}
