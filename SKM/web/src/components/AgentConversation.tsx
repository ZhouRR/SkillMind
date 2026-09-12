import { useEffect, useRef } from 'react'

import type { RunEventRecord, RunStatus } from '../api'
import {
  projectAgentStream,
  type AgentPromptSummary,
  type StructuredResultDigest,
} from '../lib/agentStream'
import { useMessages } from '../i18n'
import { isNearBottom } from '../lib/scroll'
import { EmptyState } from './PageElements'

/** User task 要約と Agent の完成・streaming text を会話形式で表示する。 */
export function AgentConversation({ prompt, events, runStatus }: {
  prompt: AgentPromptSummary | null
  events: RunEventRecord[]
  runStatus: RunStatus | null
}) {
  const messages = useMessages()
  const stream = projectAgentStream(events)
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinnedToBottom = useRef(true)
  // 完成 message と streaming delta の伸長に追随して末尾へスクロールする。上へ離れている間は追随しない。
  useEffect(() => {
    const element = scrollRef.current
    if (element && pinnedToBottom.current) element.scrollTop = element.scrollHeight
  }, [stream.completed.length, stream.partial])
  if (prompt === null) {
    return <EmptyState text={messages.conversation.emptyBeforeRun} />
  }
  const sourceEntries = Object.entries(prompt.sources)
  const inputEntries = Object.entries(prompt.input)
  return (
    <div
      className="conversation"
      aria-live="polite"
      ref={scrollRef}
      onScroll={(event) => { pinnedToBottom.current = isNearBottom(event.currentTarget) }}
    >
      <article className="message messageUser">
        <div className="messageMeta"><strong>{messages.conversation.taskRequest}</strong><span>{messages.conversation.userRole}</span></div>
        <p>
          {messages.conversation.taskSentencePrefix}<strong>{prompt.taskTitle}</strong>
          {prompt.capability && <> (<code>{prompt.capability}</code>)</>}
          {messages.conversation.taskSentenceSuffix}
        </p>
        {inputEntries.length > 0 && (
          <dl>
            {inputEntries.map(([key, value]) => (
              <div key={key}><dt>{key}</dt><dd>{formatInputValue(value)}</dd></div>
            ))}
          </dl>
        )}
        {sourceEntries.length > 0 && (
          <dl className="promptSources">
            {sourceEntries.map(([key, provider]) => (
              <div key={key}><dt>{key}</dt><dd>{provider}</dd></div>
            ))}
          </dl>
        )}
      </article>
      {stream.completed.map((message) => (
        <article className="message messageAgent" key={message.sequence}>
          <div className="messageMeta"><strong>{messages.conversation.agentOutput}</strong><span>#{message.sequence}</span></div>
          {message.kind === 'text'
            ? <pre>{message.text}</pre>
            : <StructuredResultMessage digest={message.digest} />}
        </article>
      ))}
      {stream.partial && (
        <article className="message messageAgent messageStreaming">
          <div className="messageMeta"><strong>{messages.conversation.agentStreaming}</strong><span>{messages.conversation.realtime}</span></div>
          {stream.partialKind === 'structured'
            ? (
              <p className="structuredStreaming">
                {messages.conversation.structuredStreaming(stream.partial.length)}
                <i className="streamCursor" aria-hidden="true" />
              </p>
            )
            : <pre>{stream.partial}<i className="streamCursor" aria-hidden="true" /></pre>}
        </article>
      )}
      {stream.completed.length === 0 && !stream.partial && (
        <p className="streamWaiting">
          {runStatus === 'FAILED' || runStatus === 'CANCELLED'
            ? messages.conversation.noAgentText
            : messages.conversation.waitingAgentText}
        </p>
      )}
    </div>
  )
}

/** 任意 task input の一値を、本文を膨らませない短い表示 text へ変換する。 */
function formatInputValue(value: unknown): string {
  if (typeof value === 'string') return value || '""'
  if (typeof value === 'number' || typeof value === 'boolean' || value === null) return String(value)
  return JSON.stringify(value)
}

/** JSON 結果本文を raw で表示せず、会話向けの要約 card として表示する。 */
function StructuredResultMessage({ digest }: { digest: StructuredResultDigest }) {
  const messages = useMessages()
  const facts = [
    messages.conversation.factTopLevel(digest.topLevelFields),
    messages.conversation.factArrays(digest.arrayFields, digest.arrayItems),
    messages.conversation.factObjects(digest.objectFields),
    messages.conversation.factScalars(digest.scalarFields),
  ]
  return (
    <div className="structuredDigest">
      <p>{messages.conversation.structuredDone}</p>
      {facts.length > 0 && <ul className="digestFacts">{facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>}
    </div>
  )
}
