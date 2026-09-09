import type { AppRoute } from '../routing'

import { EN } from './en'
import { JA } from './ja'
import { ZH } from './zh'

/** UI 言語の許可集合。backend の `users.ui_language` check 制約と同一に保つ。 */
export const UI_LANGUAGES = ['zh', 'ja', 'en'] as const

/** 選択可能な UI 言語 code。 */
export type UiLanguage = (typeof UI_LANGUAGES)[number]

/** 画面が参照する User-facing 文案 catalog の形。

    key を追加する場合は三言語すべてへ同時に追加する。Record<UiLanguage, UiMessages>
    の型検査が欠落を compile error として検出する。 */
export interface UiMessages {
  /** Route ごとの表示名と概要。導航、browser title、概览 card が共有する。 */
  routes: Record<AppRoute, { label: string; description: string }>
  /** Project 非依存の本人操作・組織ユーザー管理。Server error 本文は表示しない。 */
  account: {
    myAccount: string
    manageUsers: string
    draftMemoryOnly: string
    create: string
    edit: string
    search: string
    searchPlaceholder: string
    refresh: string
    save: string
    busy: string
    emptyUsers: string
    emptyEvents: string
    close: string
    rolePlaceholder: string
    fields: Record<'email' | 'name' | 'role' | 'status' | 'userId' | 'version' | 'created' | 'updated', string>
    roles: Record<'ADMIN' | 'USER', string>
    statuses: Record<'ACTIVE' | 'DISABLED', string>
    currentPassword: string
    newPassword: string
    confirmPassword: string
    initialPassword: string
    passwordPolicy: string
    passwordMismatch: string
    changePassword: string
    passwordHint: string
    revoke: string
    revokeHint: string
    confirmRevoke: string
    confirmChange: string
    securityEvents: string
    auditHint: string
    eventActor: string
    eventRequest: string
    eventPrevious: string
    eventResult: string
    eventActions: Record<'CREATED' | 'UPDATED' | 'PASSWORD_CHANGED' | 'SESSIONS_REVOKED', string>
    revokedCount: (count: number) => string
    previous: string
    next: string
    page: (offset: number, count: number, total: number) => string
    versionUsed: (version: number) => string
    latestVersion: (version: number) => string
    reviewTitle: string
    reviewHint: string
    adoptLatest: string
    unknownHint: string
    checkCreation: string
    beginNewCreate: string
    creationReviewHint: string
    creationOriginalEmail: string
    createHint: string
    createdSuccess: string
    mutationSuccess: string
    sessionEnded: string
    retryAfter: (seconds: number) => string
    failures: Record<'sessionExpired' | 'passwordRejected' | 'csrfRejected' | 'adminRequired' | 'notFound' | 'versionConflict' | 'lastAdmin' | 'emailConflict' | 'invalidRequest' | 'rateLimited' | 'unavailable' | 'unknown' | 'loadFailed', string>
  }
  nav: {
    openMenu: string
    closeMenu: string
    menuLabel: string
    brandAriaHome: string
    mainNavAria: string
    platformGroup: string
    currentProject: string
    projectModulesAria: string
    logout: string
    apiDocs: string
    /** Logo 下の製品副題。ブランド語のため言語ごとに利用者向けの言い換えを許す。 */
    brandTagline: string
  }
  /** Backend 契約の enum 値を利用者向け語へ写像する。


      key は契約の enum 値そのもの。未収録の値は呼び出し側が `?? 原文` で fallback し、
      契約に新値が増えても画面が壊れないことを保証する。 */
  enums: {
    /** Run status(QUEUED〜CANCELLED)。badge と状態行が共有する。 */
    runStatus: Record<string, string>
    /** Segment/AgentSession の status(CREATED〜CANCELLED は両者同一語彙)。 */
    sessionStatus: Record<string, string>
    /** RunSegment の開始契機。 */
    segmentTrigger: Record<string, string>
    /** Session 継続方式(INITIAL/RESUME/FORK/REPLACE)。 */
    continuationMode: Record<string, string>
    sessionKind: Record<string, string>
    /** UserInteraction の種別。 */
    interactionType: Record<string, string>
    /** UserInteraction の status。 */
    interactionStatus: Record<string, string>
    /** ChangeProposal の status。 */
    proposalStatus: Record<string, string>
    /** EffectExecution の status。 */
    effectStatus: Record<string, string>
    /** 効果の risk 等級。 */
    riskLevel: Record<string, string>
    /** 管理資源(Secret/Integration/Binding/Policy)の status。 */
    resourceStatus: Record<string, string>
    /** SkillVersion の status。 */
    skillVersionStatus: Record<string, string>
    /** TaskSchedule の status。 */
    scheduleStatus: Record<string, string>
    /** TaskSchedule の発火形態。 */
    scheduleKind: Record<string, string>
    /** 扇出子 Agent 一路の結末。 */
    subagentOutcome: Record<string, string>
    /** 人手評価の verdict(小文字契約値)。 */
    verdict: Record<string, string>
    /** 監査 event の契約名を利用者向けの行動語へ写像する。 */
    runEvent: Record<string, string>
  }
  service: {
    connecting: string
    failed: string
    ok: (version: string) => string
  }
  app: {
    projectUnavailable: string
    projectReadFailed: string
    projectArchived: string
    verifyingSession: string
    cannotVerifySession: string
    sessionExpired: string
    loadProjectsFailed: string
    loadPreferenceFailed: string
    savePreferenceFailed: string
    saveLanguageFailed: string
    logoutFailed: string
  }
  login: {
    title: string
    subtitle: string
    email: string
    password: string
    submit: string
    submitting: string
    failed: string
    rateLimited: string
    retryAfter: (seconds: number) => string
    unavailable: string
    invalidCredentials: string
    csrfRejected: string
    footer: string
  }
  home: {
    description: string
    statusSectionAria: string
    serviceStatus: string
    connectFailed: string
    connectingShort: string
    running: string
    readingServiceInfo: string
    currentProject: string
    notSelected: string
    selectProjectHint: string
    executionBoundary: string
    readOnlyTools: string
    boundaryHint: string
    projectReady: string
    recentRuns: string
    goTasks: string
    readingHistory: string
    loadRunsFailed: string
    emptyNoProject: string
    emptyNoRuns: string
  }
  language: {
    label: string
    names: Record<UiLanguage, string>
  }
  /** 共有 UI 部品(PageElements)の既定文言。 */
  elements: {
    projectUnavailable: string
    archivedProject: string
    projectLabel: string
    noAccessibleProjects: string
    selectProject: string
    projectListFailed: string
    loadingProjects: string
    loading: string
    detail: string
    processing: string
    /** 監査 event 詳細の識別ラベル群。値(ID/時刻)は契約の原文のまま表示する。 */
    eventRun: string
    eventType: string
    eventAttempt: string
    eventSession: string
    eventOccurredAt: string
    eventTrace: string
    eventPayload: string
    /** 結果要約が無い Run の一覧見出し。短縮 ID を受け取って表示名を組み立てる。 */
    runFallbackTitle: (shortId: string) => string
    /** 共通 modal(ModalDialog)の閉じる操作。 */
    close: string
    /** 確認 dialog(ConfirmDialog)の取消操作。実行側の label は呼び出し元が渡す。 */
    cancel: string
  }
  /** Agent 会話 view(AgentConversation)。 */
  conversation: {
    emptyBeforeRun: string
    taskRequest: string
    /** 依頼者側 message の話者ラベル。 */
    userRole: string
    taskSentencePrefix: string
    taskSentenceSuffix: string
    agentOutput: string
    agentStreaming: string
    realtime: string
    structuredStreaming: (receivedChars: number) => string
    noAgentText: string
    waitingAgentText: string
    structuredDone: string
    factTopLevel: (count: number) => string
    factArrays: (count: number, items: number) => string
    factObjects: (count: number) => string
    factScalars: (count: number) => string
    digestHint: string
  }
  /** Run 履歴一覧(RunHistoryPanel)。 */
  runHistory: {
    loading: string
    retry: string
    empty: string
    noSummary: string
    sourceUnavailable: string
    previous: string
    next: string
  }
  /** Project 全体の実行記録を Workspace から切り離して閲覧する画面。 */
  historyPage: {
    description: string
    hint: string
    scopeBadge: string
    openWorkspace: string
    aria: string
  }
  /** Schema 駆動 task 入力 form(SchemaTaskInput)。 */
  taskInput: {
    advancedJson: string
    jsonSuffix: string
    rawLabel: string
  }
  /** 項目文書画面(DocumentsPage)。 */
  documentsPage: {
    description: string
    scopeBadge: string
    /** 画面全体の aria-label。 */
    pageAria: string
  }
  /** 項目管理画面(ProjectsPage)と module 設定区画。 */
  projects: {
    description: string
    /** 管理対象別 tab。成員管理は ADMIN のみに追加する。 */
    pageTabsAria: string
    tabProjects: string
    tabArchived: string
    tabModules: string
    modulesNeedProject: string
    accessible: string
    loadingList: string
    emptyActive: string
    noDescription: string
    archive: string
    archiveConfirm: (name: string) => string
    createFailed: string
    archiveFailed: string
    createTitle: string
    keyLabel: string
    nameLabel: string
    descriptionLabel: string
    retentionLabel: string
    createSubmit: string
    /** Project metadata の編集(key と status は対象外)。 */
    edit: string
    editTitle: string
    saveChanges: string
    cancelEdit: string
    updateFailed: string
    /** アーカイブ済み Project の可視化・復元・key 解放のための削除。 */
    archivedAria: string
    archivedTitle: string
    archivedHint: string
    archivedEmpty: string
    loadingArchived: string
    loadArchivedFailed: string
    restore: string
    restoreFailed: string
    deleteProject: string
    deleteConfirm: (name: string, key: string) => string
    deleteFailed: string
    /** 削除拒否の理由。backend は Problem code で両者を区別する。 */
    deleteBlockedByRuns: string
    deleteBlockedBySchedules: string
    deleteBlockedByMemberAudit: string
    deleteNeedsArchive: string
    permissionsTitle: string
    permissionsHint: string
    goSkills: string
    goWorkspace: string
    modules: {
      sectionAria: string
      title: string
      hint: string
      loading: string
      empty: string
      edit: string
      remove: string
      removeConfirm: (name: string) => string
      saveFailed: string
      removeFailed: string
      createTitle: string
      editTitle: string
      nameLabel: string
      descriptionLabel: string
      pickerLegend: string
      noPublished: string
      /** 廃止・無効化で候補から外れたのに束縛が残っている version の注記。 */
      staleBinding: string
      saveChanges: string
      cancelEdit: string
      configTitle: string
      configHint: string
    }
  }
  /** Project CRUD の原版・草稿・現在値と、未知結果の明示的照合。 */
  projectManagement: {
    version: string
    status: string
    action: string
    actions: Record<'create' | 'edit' | 'archive' | 'restore' | 'delete', string>
    states: Record<'ACTIVE' | 'ARCHIVED', string>
    original: string
    draft: string
    current: string
    newIdentity: string
    notAccessibleFact: string
    noKeyMatch: string
    settingsPreserved: string
    confirmTitle: string
    confirm: string
    archiveHint: string
    restoreHint: string
    deleteHint: string
    saved: string
    conflictTitle: string
    conflictHint: string
    readOriginal: string
    adopt: string
    unknownTitle: string
    unknownHint: string
    factLimit: string
    acknowledgeCheck: string
    acknowledge: string
    acknowledgedHint: string
    failures: Record<'sessionExpired' | 'csrfRejected' | 'adminRequired' | 'notFound' | 'invalidRequest'
      | 'keyConflict' | 'versionConflict' | 'versionExhausted' | 'needsArchive' | 'blockedByRuns'
      | 'blockedBySchedules' | 'blockedByMemberAudit' | 'unknown' | 'loadFailed', string>
  }
  /** Project membership と Organization account の状態を混同しない管理区画。 */
  projectMembers: {
    tab: string
    title: string
    description: string
    needProject: string
    adminOnly: string
    adminBypass: string
    archivedHint: string
    relationshipHint: string
    membersTitle: string
    emptyMembers: string
    relationship: string
    joinedAt: string
    states: Record<'ACTIVE' | 'REMOVED' | 'ABSENT', string>
    candidatesTitle: string
    search: string
    searchHint: string
    emptyCandidates: string
    add: string
    remove: string
    alreadyMember: string
    disabledCandidate: string
    intentTitle: string
    confirmAdd: string
    confirmRemove: string
    confirm: string
    selectionChanged: string
    saved: string
    unknownTitle: string
    unknownHint: string
    reconcile: string
    reconcileHint: string
    originalAction: string
    originalRelationship: string
    currentRelationship: string
    acknowledgeCheck: string
    acknowledge: string
    acknowledgedHint: string
    failures: Record<'sessionExpired' | 'csrfRejected' | 'adminRequired' | 'notFound' | 'invalidRequest' | 'unknown' | 'loadFailed', string>
  }
  /** 資源と統合の管理画面(ResourcesPage)。 */
  resources: {
    title: string
    description: string
    scopeBadge: string
    selectProjectFirst: string
    loadFailed: string
    mutationFailed: string
    loadingConfig: string
    /** 接続 form:凭据登録と Integration 作成を一回の提交へ束ねる。 */
    connectTitle: string
    connectHint: string
    nameLabel: string
    providerLabel: string
    baseUrlLabel: string
    repositoryUriLabel: string
    defaultRevisionLabel: string
    writeModeLabel: string
    writeModeDirect: string
    writeModeBranch: string
    writeModeDirectHint: string
    writeModeBranchHint: string
    writeBranchPrefixLabel: string
    forgeKindLabel: string
    forgeNone: string
    forgeApiBaseUrlLabel: string
    forgeProjectLabel: string
    forgeHint: string
    writeConfigIssue: Record<
      'direct_requires_branch_name' | 'branch_prefix_reserved' | 'forge_incomplete',
      string
    >
    credentialLabel: string
    credentialNew: string
    notUsed: string
    secretHint: string
    resolverLabel: string
    resolverEnvOption: string
    resolverFileOption: string
    resolverManagedOption: string
    locatorFieldLabel: string
    secretValueLabel: string
    secretValuePlaceholder: string
    secretValueHint: string
    tabsAria: string
    tabConnect: string
    tabSecret: string
    tabBinding: string
    tabPolicy: string
    accessLabel: string
    accessRead: string
    accessReadWrite: string
    accessHint: string
    /** 範囲の既定は明示 wildcard(不限)。列挙は任意の絞り込みとして残す。 */
    issueScopeLabel: string
    issueScopeAllOption: string
    issueScopeListOption: string
    fieldScopeLabel: string
    fieldScopeAllOption: string
    fieldScopeListOption: string
    issueIdsLabel: string
    issueIdsHint: string
    fieldKeysHint: string
    customFieldKeysLabel: string
    pathsLabel: string
    revisionsLabel: string
    connectSubmit: string
    /** Server が確実に拒否する scope を送信前に知らせる検証文言。 */
    issueIdsRequired: string
    fieldKeysRequired: string
    pathsRequired: string
    /** 接続済み一覧と共通操作。 */
    integrationListTitle: string
    connectGuide: string
    accessBadgeRead: string
    accessBadgeReadWrite: string
    notConfigured: string
    disable: string
    /** 折り畳み表示の高度設定(凭据管理・既定 binding・書き込み事前許可)。 */
    secretTitle: string
    secretAdvancedHint: string
    keyVersionLabel: string
    registerLocator: string
    /** 一覧主体 + 新規 modal の各区分で使う新規作成導線。 */
    newBinding: string
    newPolicy: string
    bindingTitle: string
    bindingHint: string
    levelLabel: string
    projectDefault: string
    taskOverride: string
    taskSelectLabel: string
    taskScopeKeyLabel: string
    requirementKeyLabel: string
    requirementCustomOption: string
    requirementKeyInputLabel: string
    integrationSelectLabel: string
    derivedCapabilityLabel: string
    capabilitySelectLabel: string
    noCapabilityForRequirement: string
    scopeSubsetLabel: string
    scopeKeepAllOption: string
    scopeNarrowHint: string
    scopeUnrestrictedLabel: string
    pleaseSelect: string
    selectValidIntegration: string
    saveBinding: string
    policyTitle: string
    policyHint: string
    operationLabel: string
    operationHint: string
    expiresLabel: string
    createPolicy: string
    noWritableIntegration: string
  }
  /** 項目文書 panel(DocumentManagerPanel)。 */
  documentsPanel: {
    unknownTitle: string
    factsOnly: string
    checkOriginal: string
    checking: string
    present: string
    absent: string
    release: string
    refresh: string
    failures: Record<'sessionExpired' | 'denied' | 'archived' | 'notFound' | 'inUse'
      | 'referencesUnavailable' | 'invalid' | 'unknown' | 'loadFailed'
      | 'previewTooLarge' | 'contentMissing' | 'contentInvalid' | 'storageUnavailable'
      | 'uploadTooLarge' | 'uploadUnknown', string>
    selectProjectFirst: string
    listTitle: string
    hint: string
    chooseFiles: string
    chooseFolder: string
    uploadFilesAria: string
    uploadFolderAria: string
    uploadFailed: string
    failureJoin: string
    uploading: (done: number, total: number) => string
    deleteConfirm: (name: string) => string
    loadingDocs: string
    emptyDocs: string
    oversizedTitle: string
    previewButton: string
    download: string
    deleting: string
    remove: string
    close: string
    loadingPreview: string
    previewNotice: string
    /** 文書 panel の aria-label。 */
    panelAria: string
  }
  /** Run 結果・監査 panel(RunResultPanel)。 */
  /** 普通答復の原要求と読取事実を混同しないための表示。 */
  interactionResponse: {
    recordsTitle: string
    showRecord: string
    hideRecord: string
    effectReadOnly: string
    draftSeparate: string
    answerTooLong: string
    duplicateOptions: string
    staleDetail: string
    originalVersion: (version: number) => string
    originalActor: string
    originalTarget: string
    unknownHint: string
    confirmHint: string
    confirmOriginal: string
    editAnswer: string
    responseId: string
    continuationId: string
    readOriginal: string
    reading: string
    factsOnly: string
    currentState: string
    notInDetail: string
    notConfirmation: string
    phase: Record<'sending' | 'unknown' | 'rejected' | 'conflict' | 'expired' | 'confirmed', string>
    failures: Record<'sessionExpired' | 'csrfRejected' | 'projectArchived' | 'forbidden' | 'notFound' | 'invalidAnswer' | 'conflict' | 'expired' | 'unknown' | 'loadFailed', string>
  }
  runResult: {
    /** 凍結文書の検証状態。実行成功や現在の blob 可達性と混同しない。 */
    documents: {
      title: string
      hint: string
      status: Record<'FROZEN' | 'LEGACY_UNAVAILABLE' | 'INVALID', string>
      legacy: string
      invalid: string
      checksum: string
      members: (count: number) => string
      documentId: string
      contentHash: string
      size: (bytes: number) => string
    }
    /** 未応答の質問・未決の変更提案を結果より上へ集約する区画。 */
    pendingTitle: string
    pendingHint: string
    idleEmpty: string
    loadingEmpty: string
    noValidatedResult: (status: string) => string
    /** 技術 field key/contract identifier を必要なときだけ表示する。 */
    technicalDetails: string
    hideTechnicalDetails: string
    needsReview: string
    noExtraReview: string
    genericOutcome: string
    structuredResult: string
    toolCalls: string
    noToolCalls: string
    noEvidence: string
    viewRawResult: string
    /** 長い一覧の尾部を畳んだときの展開行。件数を受け取り、総量を隠さない。 */
    showRemaining: (count: number) => string
    controlledEffects: string
    approvalReason: string
    approveAndApply: string
    rejectAndContinue: string
    proposalExpired: string
    evidenceLine: (refs: string[], checksum: string) => string
    changeBaseRevision: (revision: string, files: number) => string
    changeAction: Record<string, string>
    changeSize: (lines: number, bytes: number) => string
    changeRemoved: string
    conversationAudit: string
    segmentObjectiveFallback: string
    interactionNeedInput: string
    impactLine: (impact: string) => string
    deadlineLine: (expiresAt: string, mode: string) => string
    answeredLine: (answer: string) => string
    effectApprovalHint: string
    recommendedSuffix: string
    yourAnswer: string
    choiceNote: string
    submitting: string
    submitAndContinue: string
    deliverables: string
    noDeliverables: string
    findingsTitle: string
    noFindings: string
    openQuestions: string
    noOpenQuestions: string
    limitations: string
    noLimitations: string
    businessStructured: string
    changesAndEffects: string
    manualEvaluation: string
    loadingEvaluations: string
    noEvaluations: string
    ratingLabel: string
    verdictLabel: string
    verdictAccurate: string
    verdictPartial: string
    verdictInaccurate: string
    verdictUncertain: string
    commentLabel: string
    addRevision: string
    suggestedValueLabel: string
    revisionReasonLabel: string
    saving: string
    addEvaluation: string
    aiOriginal: string
    humanSuggestion: string
    emptyArray: string
    viewExcerpt: string
    noExcerpt: string
    /** 結果要約・提案・監査行の識別ラベル群(旧 hard code 英語の資源化)。 */
    summaryLabel: string
    confidenceLabel: string
    reviewLabel: string
    schemaCheckLabel: string
    schemaValidText: string
    schemaCheckRequiredText: string
    taskVersionLabel: string
    formatBadgeOutcome: string
    formatBadgeSchema: string
    formatBadgeLegacy: string
    evidenceTitle: string
    evidenceSourceLabel: string
    evidenceLocatorLabel: string
    evidenceHashLabel: string
    /** 扇出（並行子分析）。未完了の面があると結論の根拠が欠ける。 */
    subagentTitle: string
    subagentBranchCount: (count: number) => string
    subagentIncompleteWarning: (keys: string[]) => string
    subagentBudget: (turns: number, bytes: number) => string
    segmentTitle: (segmentNo: number) => string
    attemptsCount: (count: number) => string
    sessionsCount: (count: number) => string
    sessionLabel: string
    sessionParentPrefix: string
    attemptNo: (attemptNo: number) => string
    approvalPending: string
    beforeAfterLine: (before: string, after: string) => string
    targetLabel: string
    capabilityLabel: string
    versionLabel: string
    expiresLabel: string
    /** 承認理由 textarea の既定文(利用者がそのまま送信し得るため利用者言語で保持)。 */
    defaultApprovalReason: string
    jsonPointerLabel: string
    outcomeUnknown: string
    untitledLabel: string
  }
  /** 工作空間画面(WorkspacePage)。 */
  workspace: {
    selectProjectFirst: string
    titleWithModule: (moduleName: string) => string
    description: string
    scopeBadge: (scopeName: string) => string
    newRun: string
    /** 左 rail の実行入口 card と弹窗導線。form 本体は modal に移した。 */
    newRunIntro: string
    openNewRun: string
    runnableCount: (count: number) => string
    noPublishedTasks: string
    noModuleTasks: string
    /** 模块設定(項目管理)への誘導 link 文言。 */
    goModuleSettings: string
    taskLabel: string
    optionalSuffix: string
    selectConfiguredResource: string
    notUsed: string
    /** 資源種別の友好名。文書は候補が一件でも明示選択を要求する。 */
    resourceKind: Record<string, string>
    willUseSource: (label: string) => string
    sourceNotConfigured: string
    freezeHint: string
    /** 即時実行・調度が共用する文書範囲の選択と失効案内。 */
    documentSelection: {
      mode: string
      choose: string
      single: string
      set: string
      all: string
      invalid: string
      search: string
      count: (count: number) => string
      setHint: string
      noMatches: string
      allHint: string
      freezeHint: string
      incompleteDraft: string
    }
    inputValidatedHint: string
    creating: string
    startRun: string
    /** 未確認作成と新しい草稿を混同させない、三語の案内と明示確認。 */
    submission: {
      title: string
      open: string
      originalTask: (title: string) => string
      phase: Record<'sending' | 'unknown' | 'rejected' | 'conflict', string>
      mayHaveCreated: string
      memoryOnly: string
      retry: string
      history: string
      acknowledgeNew: string
      startNew: string
      unavailable: string
    }
    selectTaskFirst: string
    inputMustBeJson: string
    history: string
    /** 模块に属さない作用域を表す語。実行履歴 badge と模块未設定時の見出し badge が共有する。 */
    projectWideScope: string
    /** 模块選択中に、絞り込みが起動できる task にだけ効くことを補足する注記。 */
    historyModuleHint: string
    refresh: string
    runStatus: string
    emptyBeforeRun: string
    statusLabel: string
    /** Run 事実行の識別ラベルと画面 aria(旧 hard code 英語の資源化)。 */
    runIdLabel: string
    connLabel: string
    taskExecutionAria: string
    refreshDb: string
    cancelling: string
    cancelRun: string
    observationAria: string
    tabConversation: string
    tabResult: string
    tabEvents: string
    sseEmpty: string
    readinessTitle: (label: string) => string
    readinessLevels: Record<string, string>
    requirementStatus: Record<string, string>
    /** 就緒 status ごとの説明。backend の英語固定 reason を表示言語で置き換える。 */
    requirementReason: Record<string, string>
    noResourceNeeded: string
    requiredLabel: string
    optionalLabel: string
    candidatesLine: (labels: string[]) => string
    guidanceOnlyHint: string
    connFinished: string
    connReconnecting: string
    connStreaming: string
    connIdle: string
  }
  /** 待你处理 (PendingActionsPanel)。応答・承認待ちの Run を全画面から見える形にする。 */
  pending: {
    title: string
    empty: string
    selectProjectFirst: string
    loadFailed: string
  }
  /** Project 全体の調度一覧と原詳細。編集の文案は独立した namespace に置く。 */
  scheduleManager: {
    manageAll: string; tasksLink: string; needProject: string; readOnlyProject: string; scopeHint: string
    listTitle: string; searchLabel: string; searchPlaceholder: string; invalidSearch: string; search: string; statusLabel: string; allStates: string
    refresh: string; loading: string; empty: string; total: (total: number) => string
    pagination: string; previous: string; next: string; page: (offset: number, limit: number, total: number) => string
    detailTitle: string; selectSchedule: string; loadingDetail: string; taskLoading: string; catalogUnavailable: string
    catalogRetry: string; previousFacts: string; taskUnavailable: string; guidanceOnly: string; readinessUnconfirmed: string; edit: string; controlHint: string
    editorPending: string; reopenEditor: string; definitionTitle: string; summaryTitle: string; summaryHint: string
    inputTitle: string; sourcesTitle: string; unlimited: string
    fields: Record<'id' | 'task' | 'version' | 'rowVersion' | 'creator' | 'created' | 'updated' | 'runCount' | 'missedCount' | 'nextAt' | 'lastAt' | 'lastOutcome' | 'lastRun', string>
    outcomes: Record<'RUN_CREATED' | 'SKIPPED_OVERLAP' | 'FAILED_PRECONDITION' | 'COMPLETED', string>
    failures: Record<'sessionExpired' | 'accessUnavailable' | 'loadFailed', string>
  }
  /** 任务中心 (TasksPage)。「何を走らせられるか」を選ぶ画面。 */
  tasks: {
    description: string
    titleWithModule: (moduleName: string) => string
    countBadge: (count: number) => string
    selectProjectFirst: string
    loading: string
    loadFailed: string
    empty: string
    resourcesLabel: string
    requirementCount: (count: number) => string
    scheduleLabel: string
    noSchedule: string
    scheduleCount: (count: number) => string
    nextRunLabel: string
    lastRunLabel: string
    neverRun: string
    runNow: string
    addSchedule: string
  }
  /** 時刻起動 (ScheduleDialog)。用語は一般利用者路線に合わせ、cron/schedule を表に出さない。 */
  schedules: {
    title: string
    create: string
    empty: string
    /** 凍結対象の task を明示する。調度は保存時点の設定を固定し、後から差し替えない。 */
    frozenTask: (taskTitle: string) => string
    noTaskDraft: string
    nameLabel: string
    kindLabel: string
    timezoneLabel: string
    cronLabel: string
    runAtLabel: string
    endAtLabel: string
    maxRunsLabel: string
    preview: string
    previewHint: (timezone: string) => string
    inputTimezone: (timezone: string) => string
    ruleTimezoneHint: string
    invalidLocalTime: string
    ambiguousLocalTime: string
    chooseOffset: string
    previewConfirm: string
    previewRequired: string
    previewLoading: string
    invalidDefinition: string
    previewFailed: string
    saveRejected: string
    saveDenied: string
    saveUnknown: string
    closingHint: string
    /** 重なりは既定で見送る (計画 §22 D5)。並行実行しないことを事前に伝える。 */
    overlapHint: string
    noNextRun: string
    runCount: (count: number) => string
    missedCount: (count: number) => string
    pause: string
    resume: string
    archive: string
    save: string
    saving: string
  }
  /** 在途 occurrence の読取専用投影。lease と実行停止を同一視しない。 */
  scheduleActivity: {
    title: string; scopeHint: string; refresh: string; loading: string; checkedAt: string
    legacy: string; empty: string; emptyLimit: string; pendingTitle: string
    occurrenceId: string; occurrenceAt: string; originalConfiguration: string; currentConfiguration: string
    rowVersion: string; createdAt: string; updatedAt: string; attempts: string; leaseExpiresAt: string
    leaseActive: string; leaseExpired: string; leaseLimit: string
    attemptsRemaining: string; attemptsReached: string; configurationHint: string
  }
  /** 保存済み Schedule の原版比較。manager の一覧語彙とは独立する。 */
  scheduleEditor: {
    title: string
    identity: (id: string, version: number) => string
    originalInstant: (instant: string) => string
    retainedSource: (source: string) => string
    sourceUnavailable: string
    unavailable: string
    conflictTitle: string
    unknownTitle: string
    original: string
    submitted: string
    current: string
    factLimit: string
    reconcile: string
    reading: string
    adopt: string
    previousUnknown: string
    closingHint: string
    failures: { conflict: string; unknown: string; rejected: string; denied: string; readFailed: string }
  }
  /** Skills 解析画面(SkillsPage)。 */
  skills: {
    description: string
    scopeBadgeWithProject: string
    scopeBadgeNoProject: string
    loadLibraryFailed: string
    loadEnablementsFailed: string
    interpretFailedCode: (code: string) => string
    deprecateFailed: string
    enableFailed: string
    disableFailed: string
    deprecateConfirm: (name: string, version: string) => string
    publishWarningsConfirm: (warnings: string[]) => string
    sourceTitle: string
    skillMdPlaceholder: string
    referencesLabel: string
    referencesPlaceholder: string
    parserHint: string
    technicalDetails: string
    parsing: string
    parseSkill: string
    orUploadDir: string
    chooseSkillDir: string
    uploadDirAria: string
    uploadHint: string
    parseResult: string
    parseEmptyIdle: string
    parseRunning: string
    saving: string
    saveResult: string
    uploadingParsing: string
    interpreting: string
    forceRegenerate: string
    interpretAction: string
    createDraftFromAssisted: string
    libraryTitle: string
    libraryHint: string
    enabledCount: (count: number) => string
    noProjectBadge: string
    libraryNoProjectHint: string
    loadingEnablements: string
    loadingLibrary: string
    emptyLibrary: string
    enabledBadge: string
    disabledBadge: string
    disableFromProject: string
    enableForProject: string
    disabledAuditHint: string
    deprecateVersion: string
    /** 廃止済み版の物理削除。監査参照が残る版は backend が拒否する。 */
    deleteVersion: string
    deleteConfirm: (name: string, version: string) => string
    deleteFailed: string
    deleteBlocked: string
    uploadedCount: (count: number) => string
    clearUseManual: string
    binaryStored: string
    textTooLarge: string
    interpretRunning: string
    interpretRunningHint: string
    attemptLine: (attempt: number) => string
    promptSent: string
    modelOutput: string
    waitingModelOutput: string
    interpretationTitle: string
    reusedSuffix: string
    interpretationFailedLine: (code: string) => string
    parentPrefix: string
    adjustQuote: (text: string) => string
    requiredAnswerSuffix: string
    objectivePrefix: (key: string) => string
    successCriteria: string
    deliverablePrefix: (kind: string) => string
    resourcePrereq: string
    requiredLabel: string
    optionalLabel: string
    requiredRules: string
    recommendedSteps: string
    qualityCriteria: string
    prohibited: string
    interactionPoints: string
    effectIntents: string
    effectIntentHint: string
    hasDiff: string
    firstInterpretation: string
    noStructuralDiff: string
    viewRawDiff: string
    changedCount: (count: number) => string
    gatePassed: string
    gateFailed: string
    viewInterpretationDiff: string
    published: string
    publishVersion: string
    hardGateHint: string
    toolsUnauthorized: string
    declaredToolsLine: (tools: string) => string
    adjustLabel: string
    adjustPlaceholder: string
    adjusting: string
    adjustAndReinterpret: string
    processing: string
    createDraftFromThis: string
    /** 画面 2 分割(取込と解析 / 組織ライブラリ)と解釈詳細内の tab 群。 */
    pageTabsAria: string
    tabWorkbench: string
    detailTabsAria: string
    tabReport: string
    reportEmpty: string
    blueprintEmpty: string
    contractsEmpty: string
    /** 解析・解釈・版詳細の見出しとラベル群(旧 hard code 英語の資源化)。 */
    workspaceAria: string
    libraryAria: string
    blueprintTitle: string
    generatedContractsTitle: string
    assumptionsTitle: string
    questionsTitle: string
    sourceTracesTitle: string
    revisionDiffTitle: string
    versionHeading: (version: string) => string
    versionIdLabel: string
    manifestChecksumLabel: string
    gateLabel: string
    parseNameLabel: string
    parseAdapterLabel: string
    parseSkillKeyLabel: string
    parseConfidenceLabel: string
    parseToolsLabel: string
    metricFiles: string
    metricReferences: string
    metricScripts: string
    metricAssets: string
    savedInterpretationStatus: string
    savedSourceId: string
    savedInterpretationId: string
    interpretationIdLabel: string
    interpretationStatusLabel: string
    interpretationModelLabel: string
    interpretationConfidenceLabel: string
    contractInputTitle: string
    contractOutputTitle: string
    contractFieldRequired: string
    contractFieldOptional: string
  }
}

/** 言語 code から catalog への固定対応。catalog 本体は言語別 file(zh/ja/en.ts)に置く。 */
export const MESSAGES: Record<UiLanguage, UiMessages> = { zh: ZH, ja: JA, en: EN }
