import { useEffect, useMemo, useRef, useState } from 'react'

import { loadProjectModules, loadProjectTasks, type ProjectModuleRecord, type PublishedTaskRecord } from '../api'

const EMPTY_TASKS: PublishedTaskRecord[] = []
const EMPTY_MODULES: ProjectModuleRecord[] = []

/** 同じ Project/会話/再読取に属する task と module の一覧だけを保持する。 */
export function useWorkspaceCatalog(projectId: string, session: object, revision: number, messages: {
  selectProjectFirst: string
  modulesFailed: string
}) {
  const owner = useMemo(() => ({}), [projectId, session, revision])
  const current = useRef(owner)
  current.current = owner
  const [tasks, setTasks] = useState<{ owner: object; data: PublishedTaskRecord[]; error: string | null; loaded: boolean } | null>(null)
  const [modules, setModules] = useState<{ owner: object; data: ProjectModuleRecord[]; error: string | null; loaded: boolean } | null>(null)

  useEffect(() => {
    if (!projectId) return
    const controller = new AbortController()
    const ownsRequest = () => !controller.signal.aborted && current.current === owner
    // 二つの読取は独立して完了させ、module の失敗で取得済み task を消さない。
    void loadProjectTasks(projectId, controller.signal).then((catalog) => {
      if (ownsRequest()) setTasks({ owner, data: catalog.tasks, error: null, loaded: true })
    }).catch((error: unknown) => {
      if (ownsRequest()) setTasks({ owner, data: [], error: error instanceof Error ? error.message : 'Unknown task catalog API error', loaded: false })
    })
    void loadProjectModules(projectId, controller.signal).then((data) => {
      if (ownsRequest()) setModules({ owner, data, error: null, loaded: true })
    }).catch((error: unknown) => {
      if (ownsRequest()) setModules({ owner, data: [], error: error instanceof Error ? error.message : messages.modulesFailed, loaded: false })
    })
    return () => controller.abort()
  }, [owner])

  return {
    tasks: tasks?.owner === owner ? tasks.data : EMPTY_TASKS,
    tasksLoaded: tasks?.owner === owner && tasks.loaded,
    tasksError: !projectId ? messages.selectProjectFirst : tasks?.owner === owner ? tasks.error : null,
    modules: modules?.owner === owner ? modules.data : EMPTY_MODULES,
    modulesLoaded: modules?.owner === owner && modules.loaded,
    modulesError: modules?.owner === owner ? modules.error : null,
  }
}
