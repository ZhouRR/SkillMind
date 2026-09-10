import { describe, expect, it } from 'vitest'

import {
  EMPTY_BINDING,
  EMPTY_POLICY,
  EMPTY_SECRET,
  emptyConnectDraft,
  secretInputFromDraft,
} from '../../src/lib/resourceDrafts'

describe('secretInputFromDraft', () => {
  it('sends the plaintext only for MANAGED and never a locator alongside it', () => {
    // MANAGED は明文を一度だけ渡し、server が即座に KEK 封入する。locator を同時に送ると
    // 「どちらが正なのか」が server 側で曖昧になる。
    const input = secretInputFromDraft({
      name: 'redmine-token',
      provider: 'redmine',
      resolver: 'MANAGED',
      locator: 'IGNORED_ENV_NAME',
      secretValue: 'plaintext-token',
      keyVersion: 'v1',
    })

    expect(input).toEqual({
      name: 'redmine-token',
      provider: 'redmine',
      key_version: 'v1',
      resolver: 'MANAGED',
      secret_value: 'plaintext-token',
    })
    expect('locator' in input).toBe(false)
  })

  it('sends the locator only for non-MANAGED and never the plaintext', () => {
    // ENVIRONMENT/FILE は参照だけを持つ。明文が混ざると、保存しない前提が崩れる。
    const input = secretInputFromDraft({
      name: 'git-token',
      provider: 'git',
      resolver: 'ENVIRONMENT',
      locator: 'SKILLMIND_GIT_TOKEN',
      secretValue: 'should-not-be-sent',
      keyVersion: 'v2',
    })

    expect(input).toEqual({
      name: 'git-token',
      provider: 'git',
      key_version: 'v2',
      resolver: 'ENVIRONMENT',
      locator: 'SKILLMIND_GIT_TOKEN',
    })
    expect('secret_value' in input).toBe(false)
  })
})

describe('draft defaults', () => {
  it('defaults a repository connection to read-only', () => {
    // 既定を書き込み可にすると、気付かないまま write を宣言した Integration が生まれる。
    expect(emptyConnectDraft('git').access).toBe('read')
    expect(emptyConnectDraft('svn').access).toBe('read')
    expect(emptyConnectDraft('redmine').access).toBe('read')
  })

  it('starts every draft empty so a previous dialog never leaks into the next', () => {
    expect(EMPTY_SECRET.secret_value).toBe('')
    expect(EMPTY_BINDING.integration_id).toBe('')
    expect(EMPTY_POLICY.integration_id).toBe('')
    expect(EMPTY_POLICY.scopeDraft).toEqual([])
  })

  it('keeps the managed resolver as the default so no plaintext is left in the environment', () => {
    // 既定を ENVIRONMENT にすると、利用者は明文を .env へ置く運用へ流れる。
    expect(EMPTY_SECRET.resolver).toBe('MANAGED')
  })
})
