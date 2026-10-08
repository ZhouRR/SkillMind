import { useMessages } from '../i18n'
import { useLayoutEffect, useMemo, useRef, useState } from 'react'

/** 表示用の iframe identity。内容は key やログへ載せない。 */
let nextPreviewId = 0

/** srcdoc の旧 load を採用せず、初期・再読込中の白い canvas を親の surface で隠す。 */
export function StaticPreviewFrame({ source, title, className = 'previewFrame' }: {
  source: string; title: string; className?: string
}) {
  const messages = useMessages()
  const owner = useMemo(() => ({ id: ++nextPreviewId }), [source])
  const [loaded, setLoaded] = useState<typeof owner | null>(null)
  const active = useRef<typeof owner | null>(null)
  useLayoutEffect(() => {
    active.current = owner
    return () => { if (active.current === owner) active.current = null }
  }, [owner])
  const ready = loaded === owner
  return <div className="previewFrameSurface" aria-busy={!ready}>
    <p className="hint previewFrameNavigation">{messages.assetsAudit.frameNavigation}</p>
    <iframe tabIndex={0} key={owner.id} className={className} sandbox="" referrerPolicy="no-referrer" srcDoc={source}
      title={title} style={{ visibility: ready ? 'visible' : 'hidden' }}
      onLoad={() => { if (active.current === owner) setLoaded(owner) }} />
  </div>
}
