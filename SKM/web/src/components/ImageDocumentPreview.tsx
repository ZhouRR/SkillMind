import { useLayoutEffect, useRef, useState } from 'react'
import { useMessages } from '../i18n'
import { LoadingSkeleton } from './PageElements'

/** 検証済み raster Blob の URL と decode 状態を一つの表示 owner に閉じ込める。 */
interface ImageResource { blob: Blob; url: string; status: 'loading' | 'ready' | 'error' }

/** Blob 切替・閉鎖時に URL を破棄し、旧 img の遅延 event は次の画像へ反映しない。 */
export function ImageDocumentPreview({ blob, title }: { blob: Blob; title: string }) {
  const labels = useMessages().documentsPanel
  const [resource, setResource] = useState<ImageResource | null>(null)
  const owner = useRef<ImageResource | null>(null)
  const currentBlob = useRef(blob)
  currentBlob.current = blob
  useLayoutEffect(() => {
    let url: string
    try { url = URL.createObjectURL(blob) }
    catch { setResource({ blob, url: '', status: 'error' }); return }
    const next: ImageResource = { blob, url, status: 'loading' }
    owner.current = next
    setResource(next)
    return () => { owner.current = null; URL.revokeObjectURL(url) }
  }, [blob])
  const visible = resource?.blob === blob ? resource : null
  /** resource identity は decode 状態更新後も元 event owner と一致する。 */
  function finish(status: 'ready' | 'error'): void {
    if (!visible || owner.current?.url !== visible.url || currentBlob.current !== visible.blob) return
    setResource({ ...visible, status })
  }
  return <div className="imagePreview" aria-busy={!visible || visible.status === 'loading'}>
    {(!visible || visible.status === 'loading') && <LoadingSkeleton label={labels.loadingPreview} rows={3} />}
    {visible?.status === 'error' && <p className="error" role="alert">{labels.failures.loadFailed}</p>}
    {visible && visible.status !== 'error' && <img key={visible.url} className="previewImage"
      src={visible.url} alt={title} referrerPolicy="no-referrer"
      style={{ visibility: visible.status === 'ready' ? 'visible' : 'hidden' }}
      onLoad={() => finish('ready')} onError={() => finish('error')} />}
  </div>
}
