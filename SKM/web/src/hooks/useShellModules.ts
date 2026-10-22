import { useEffect, useMemo, useRef, useState } from 'react'

import { ApiProblemError, loadProjectModules, type ProjectModuleRecord } from '../api'

const EMPTY_MODULES: ProjectModuleRecord[] = []

/** ナビゲーションの module 選択を Project と認証世代へ束縛する。 */
export function useShellModules(projectId: string, sessionKey: string, isCurrentSession: () => boolean, onSessionEnded: () => void) {
  const owner = useMemo(() => ({ projectId, sessionKey }), [projectId, sessionKey])
  const current = useRef(owner)
  current.current = owner
  const [state, setState] = useState<{ owner: object; modules: ProjectModuleRecord[] } | null>(null)
  const [choice, setChoice] = useState<{ owner: object; moduleId: string } | null>(null)

  useEffect(() => {
    if (!projectId || !sessionKey) return
    const controller = new AbortController()
    const ownsRequest = () => !controller.signal.aborted && current.current === owner && isCurrentSession()
    void loadProjectModules(projectId, controller.signal).then((modules) => {
      if (!ownsRequest()) return
      setState({ owner, modules })
      setChoice((previous) => ({ owner, moduleId: previous?.owner === owner
        && modules.some((item) => item.module_id === previous.moduleId)
        ? previous.moduleId : modules[0]?.module_id ?? '' }))
    }).catch((error: unknown) => {
      if (!ownsRequest()) return
      if (error instanceof ApiProblemError && error.status === 401) onSessionEnded()
      // 一覧取得の障害は主ナビゲーションを止めない。
      setState({ owner, modules: [] })
    })
    return () => controller.abort()
  }, [owner])

  return {
    modules: state?.owner === owner ? state.modules : EMPTY_MODULES,
    moduleId: choice?.owner === owner ? choice.moduleId : '',
    select(moduleId: string): void {
      if (current.current === owner && isCurrentSession()) setChoice({ owner, moduleId })
    },
  }
}
