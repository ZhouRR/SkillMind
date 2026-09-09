import { useState, type ReactNode } from 'react'

import type { AuthSessionRecord, ProjectRecord } from '../api'
import type { ProjectState } from '../appState'
import { EmptyState, PageHeader } from '../components/PageElements'
import { ProjectManagementPanel } from '../components/ProjectManagementPanel'
import { ProjectMembersPanel } from '../components/ProjectMembersPanel'
import { ProjectModulesPanel } from '../components/ProjectModulesPanel'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'

export { staleBindingIds } from '../components/ProjectModulesPanel'
export { projectDeleteErrorMessage } from '../lib/projectFeedback'

/** tab は表示だけを切り替え、CRUD と membership の原要求を破棄しない。 */
type ProjectsPageTab = 'projects' | 'archived' | 'modules' | 'members'
/** App の認可済み現在値と、認可再確認中も変えない context identity を分離する。 */
interface ProjectsPageProps {
  projectId: string
  setProjectId: (projectId: string) => void
  session: AuthSessionRecord
  projectState: ProjectState
  projectContextId?: string
  managementContextKey?: string
  currentProject?: ProjectRecord | null
  onSessionEnded?: SessionEnded
  onProjectChanged: (project: ProjectRecord) => void
  onProjectArchived: (project: ProjectRecord) => void
  onProjectDeleted?: (project: ProjectRecord) => void
}

/** 同じ ID に戻っても古い session/Project の write response を再利用しない。 */
export function ProjectsPage(props: ProjectsPageProps) {
  const contextId = props.projectContextId ?? props.currentProject?.project_id ?? props.projectId
  return <ProjectsContent key={`${props.session.user.user_id.toLowerCase()}:${props.session.csrf_token}:${props.managementContextKey ?? contextId.toLowerCase()}`} {...props} projectContextId={contextId} />
}

/** Platform CRUD と Project 固有の member/module をそれぞれの境界へ接続する。 */
function ProjectsContent(props: ProjectsPageProps & { projectContextId: string }) {
  const messages = useMessages()
  const [pageTab, setPageTab] = useState<ProjectsPageTab>('projects')
  const [membersOpened, setMembersOpened] = useState(false)
  const projects = props.projectState.status === 'ready' ? props.projectState.projects : []
  const onSessionEnded = props.onSessionEnded ?? (() => {})
  /** 初回だけ member を mount し、以後の tab 往復は未知結果を維持する。 */
  function selectPageTab(tab: ProjectsPageTab): void {
    if (tab === 'members') setMembersOpened(true)
    setPageTab(tab)
  }
  return <>
    <PageHeader title={messages.routes.projects.label} description={messages.projects.description} />
    <div className="tabBar" role="tablist" aria-label={messages.projects.pageTabsAria}>
      <ProjectsTabButton current={pageTab} tab="projects" onSelect={selectPageTab}>{messages.projects.tabProjects}<span className="eventCount">{projects.length}</span></ProjectsTabButton>
      {props.session.user.system_role === 'ADMIN' && <ProjectsTabButton current={pageTab} tab="archived" onSelect={selectPageTab}>{messages.projects.tabArchived}</ProjectsTabButton>}
      <ProjectsTabButton current={pageTab} tab="modules" onSelect={selectPageTab}>{messages.projects.tabModules}</ProjectsTabButton>
      {props.session.user.system_role === 'ADMIN' && <ProjectsTabButton current={pageTab} tab="members" onSelect={selectPageTab}>{messages.projectMembers.tab}</ProjectsTabButton>}
    </div>
    <ProjectManagementPanel {...props} tab={pageTab} onSessionEnded={onSessionEnded} onEditTab={() => setPageTab('projects')} />
    <div className="tabPanel" id="project-panel-modules" aria-labelledby="project-tab-modules" role="tabpanel" hidden={pageTab !== 'modules'}>
      {props.projectId ? <ProjectModulesPanel projectId={props.projectId} session={props.session} /> : <EmptyState text={messages.projects.modulesNeedProject} />}
    </div>
    {props.session.user.system_role === 'ADMIN' && <div className="tabPanel" id="project-panel-members" aria-labelledby="project-tab-members" role="tabpanel" hidden={pageTab !== 'members'}>
      {membersOpened && <ProjectMembersPanel projectContextId={props.projectContextId} currentProject={props.currentProject} session={props.session} onSessionEnded={onSessionEnded} />}
    </div>}
  </>
}

/** 項目管理画面の区分 tab button。選択状態を aria-selected で表す。 */
function ProjectsTabButton({ current, tab, onSelect, children }: {
  current: ProjectsPageTab
  tab: ProjectsPageTab
  onSelect: (tab: ProjectsPageTab) => void
  children: ReactNode
}) {
  return (
    <button
      aria-selected={current === tab}
      aria-controls={`project-panel-${tab}`}
      className="tab"
      data-project-tab={tab}
      id={`project-tab-${tab}`}
      onClick={() => onSelect(tab)}
      onKeyDown={(event) => {
        // 同じ tablist 内だけを移動し、読込や未知状態を持つ panel は再 mount しない。
        const buttons = Array.from(event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])
        const index = buttons.indexOf(event.currentTarget)
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
          : event.key === 'ArrowRight' ? (index + 1) % buttons.length
            : event.key === 'ArrowLeft' ? (index + buttons.length - 1) % buttons.length : null
        if (next === null) return
        event.preventDefault()
        buttons[next]?.focus()
        buttons[next]?.click()
      }}
      role="tab"
      tabIndex={current === tab ? 0 : -1}
      type="button"
    >
      {children}
    </button>
  )
}
