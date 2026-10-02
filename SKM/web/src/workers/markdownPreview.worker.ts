import { createMarkdownPreviewWorkerModel, type MarkdownPreviewWorkerRequest } from '../lib/markdownPreviewWorker'

const handle = createMarkdownPreviewWorkerModel()

/** 解析中でも親は terminate() で破棄でき、原文と全頁 token は Worker 内に留まる。 */
self.onmessage = (event: MessageEvent<MarkdownPreviewWorkerRequest>): void => {
  self.postMessage(handle(event.data))
}
