import { useCallback, useEffect, useMemo, useState } from 'react'

const ENDPOINT = '/api/replay'

type Stage = { name: string; state: string }

type LateReceipt = {
  id: number
  card_id: number
  lease_id: string | null
  executor: string
  outcome: string
  stage: string | null
  error: string | null
  received_at: string
  expected_expires_at: string | null
  note: string
}

type Card = {
  id: number
  job_id: string
  job_status: string
  job_stop_position: string
  status: string
  claimable: boolean
  frozen: boolean
  frozen_by: string | null
  freeze_reason: string | null
  stages: Stage[]
  successful_stages: string[]
  failed_stages: string[]
  pending_stages: string[]
  attempts: number
  max_attempts: number
  remaining_attempts: number
  next_claim_at: string | null
  wait_seconds: number
  claim_mode: string | null
  lease: {
    id: string
    executor: string
    mode: string
    issued_at: string
    expires_at: string | null
    seconds_left: number
    pending_stages: string[]
  } | null
  backoff: { algorithm: string; initial_seconds: number; max_seconds: number } | null
  backoff_source: string
  retryable: string[]
  failure_store: string
  stop_position: string
  archive_reason: string | null
  reason: string
  created_at: string
  updated_at: string
  lease_history: {
    lease_id: string
    executor: string
    reason: string
    closed_at: string
    reverted_stages: string[]
    sealed_stages: string[]
  }[]
  late_receipts: LateReceipt[]
}

type Overview = {
  counts: Record<string, number>
  total: number
  claimable_now: number
  late_receipts: number
  policy: {
    algorithms: string[]
    claim_modes: string[]
    lease_seconds_default: number
    lease_seconds_min: number
    lease_seconds_max: number
  }
}

const cardStatuses = ['待领取', '已冻结', '领取中', '已完成', '已转存']

function stageIcon(state: string) {
  if (state === '已确认成功') return '✓'
  if (state === '执行中') return '▶'
  if (state === '未确认失败') return '!'
  return '·'
}

function formatMoment(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function relativeWait(seconds: number, nextAt: string | null) {
  if (!nextAt) return '—'
  if (seconds <= 0) return '已到期'
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (minutes > 0) return `还有 ${minutes} 分 ${rest} 秒`
  return `还有 ${rest} 秒`
}

export default function ReplayQueue() {
  const [cards, setCards] = useState<Card[]>([])
  const [overview, setOverview] = useState<Overview | null>(null)
  const [lateReceipts, setLateReceipts] = useState<LateReceipt[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [keyword, setKeyword] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})
  const [executor, setExecutor] = useState('worker-manual-1')
  const [leaseSeconds, setLeaseSeconds] = useState(120)
  const [freezeReason, setFreezeReason] = useState<Record<number, string>>({})
  const [receiptDraft, setReceiptDraft] = useState<Record<string, string>>({})
  const [showLate, setShowLate] = useState(false)
  const [fetchedAt, setFetchedAt] = useState(() => Date.now())
  const [tick, setTick] = useState(() => Date.now())

  const query = useMemo(() => {
    const params = new URLSearchParams()
    if (keyword) params.set('keyword', keyword)
    if (statusFilter) params.set('status', statusFilter)
    params.set('size', '200')
    return params.toString()
  }, [keyword, statusFilter])

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const [cardResponse, overviewResponse, lateResponse] = await Promise.all([
        fetch(`${ENDPOINT}?${query}`),
        fetch(`${ENDPOINT}/overview`),
        fetch(`${ENDPOINT}/late-receipts`),
      ])
      if (!cardResponse.ok || !overviewResponse.ok || !lateResponse.ok) {
        throw new Error('失败重放队列读取失败')
      }
      const cardPayload = await cardResponse.json()
      setCards(cardPayload.items ?? [])
      setTotal(cardPayload.total ?? 0)
      setOverview(await overviewResponse.json())
      const latePayload = await lateResponse.json()
      setLateReceipts(latePayload.items ?? [])
      setFetchedAt(Date.now())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '失败重放队列读取失败')
    }
  }, [query])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    const timer = window.setInterval(() => setTick(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  async function runAction(values: Record<string, unknown>, okMessage: string) {
    setErrorMessage('')
    setNotice('')
    try {
      const cardId = Number(values.card_id)
      delete values.card_id
      const response = await fetch(`${ENDPOINT}/cards/${cardId}/actions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(String(payload.message ?? '动作未生效'))
        return
      }
      setNotice(okMessage)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '动作请求失败')
    }
  }

  async function scheduledClaim() {
    if (!executor.trim()) {
      setErrorMessage('领取必须填写执行者标识')
      return
    }
    setErrorMessage('')
    setNotice('')
    try {
      const response = await fetch(`${ENDPOINT}/claim`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          values: { executor: executor.trim(), mode: '定时领取', lease_seconds: leaseSeconds, limit: 5 },
        }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(String(payload.message ?? '定时领取未成功'))
        return
      }
      setNotice(String(payload.message))
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '定时领取请求失败')
    }
  }

  const counts = overview?.counts ?? {}
  const statItems = [
    { label: '队列卡片总数', value: overview?.total ?? total },
    { label: '当前可领取', value: overview?.claimable_now ?? 0 },
    { label: '冻结待检查', value: counts['已冻结'] ?? 0 },
    { label: '失败转存', value: counts['已转存'] ?? 0 },
  ]

  function liveSecondsLeft(card: Card) {
    if (!card.lease) return 0
    const elapsed = Math.floor((tick - fetchedAt) / 1000)
    return Math.max(card.lease.seconds_left - Math.max(elapsed, 0), 0)
  }

  return (
    <div className="replay-panel">
      <div className="stat-row">
        {statItems.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>作业/卡片编号</span>
          <input value={keyword} placeholder="如 SPID-0001" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>卡片状态</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">全部状态</option>
            {cardStatuses.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setKeyword(''); setStatusFilter('') }}>重置条件</button>
        <button className="btn ghost" type="button" onClick={() => setShowLate((prev) => !prev)}>
          {showLate ? '收起迟到证据' : `迟到证据（${lateReceipts.length}）`}
        </button>
      </form>

      <div className="replay-toolbar">
        <label className="filter-item">
          <span>执行者标识</span>
          <input value={executor} onChange={(event) => setExecutor(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>租约有效期（{overview?.policy.lease_seconds_min ?? 10}–{overview?.policy.lease_seconds_max ?? 3600} 秒）</span>
          <input
            type="number"
            min={overview?.policy.lease_seconds_min ?? 10}
            max={overview?.policy.lease_seconds_max ?? 3600}
            value={leaseSeconds}
            onChange={(event) => setLeaseSeconds(Number(event.target.value))}
          />
        </label>
        <button className="btn primary" type="button" onClick={() => void scheduledClaim()}>
          定时领取到期卡片
        </button>
        <span className="replay-toolbar-note">
          人工立即与定时领取共用同一把锁，同一张卡片只有一方拿到本次尝试。
        </span>
      </div>

      {notice && <p className="notice-text">{notice}</p>}

      <div className="replay-list">
        {cards.map((card) => {
          const open = expanded[card.id] ?? false
          const due = card.next_claim_at ? new Date(card.next_claim_at).getTime() <= tick : false
          const outcomes = receiptDraft[`${card.id}:outcome`] ?? '成功'
          const stageDraft = receiptDraft[`${card.id}:stage`] ?? (card.lease?.pending_stages[0] ?? '')
          const errorDraft = receiptDraft[`${card.id}:error`] ?? ''
          return (
            <article
              key={card.id}
              className={`replay-card ${card.frozen ? 'is-frozen' : ''} ${card.status === '已转存' ? 'is-archived' : ''}`}
            >
              <header className="replay-card-head" onClick={() => setExpanded((prev) => ({ ...prev, [card.id]: !open }))}>
                <div className="replay-card-title">
                  <span className="replay-caret">{open ? '▾' : '▸'}</span>
                  <strong>卡片 #{card.id}</strong>
                  <span className="replay-job">关联作业 {card.job_id}</span>
                  <span className={`badge badge-${card.status}`}>{card.status}</span>
                  {card.claimable && <span className="badge badge-due">可领取</span>}
                </div>
                <div className="replay-card-meta">
                  <span>成功 {card.successful_stages.length} 阶段</span>
                  <span>失败：{card.failed_stages.length ? card.failed_stages.join('、') : '无'}</span>
                  <span>剩余 {card.remaining_attempts}/{card.max_attempts} 次</span>
                  <span>
                    下次领取 {formatMoment(card.next_claim_at)}（{relativeWait(card.wait_seconds, card.next_claim_at)}）
                  </span>
                </div>
              </header>

              {open && (
                <div className="replay-card-body">
                  <div className="replay-stages">
                    {card.stages.map((stage) => (
                      <div key={stage.name} className={`replay-stage stage-${stage.state}`}>
                        <span className="stage-icon">{stageIcon(stage.state)}</span>
                        <span className="stage-name">{stage.name}</span>
                        <span className="stage-state">{stage.state}</span>
                      </div>
                    ))}
                  </div>

                  <dl className="replay-detail">
                    <div><dt>作业状态</dt><dd>{card.job_status}</dd></div>
                    <div><dt>停止位置</dt><dd>{card.job_stop_position || card.stop_position || '—'}</dd></div>
                    <div>
                      <dt>退避快照</dt>
                      <dd>
                        {card.backoff
                          ? `${card.backoff.algorithm} / 初始 ${card.backoff.initial_seconds}s / 上限 ${card.backoff.max_seconds}s（${card.backoff_source || '无策略来源'}）`
                          : '—'}
                      </dd>
                    </div>
                    <div><dt>可重试异常</dt><dd>{card.retryable.length ? card.retryable.join('、') : '—'}</dd></div>
                    <div><dt>失败转存</dt><dd>{card.failure_store || '—'}</dd></div>
                    {card.lease && (
                      <div>
                        <dt>当前租约</dt>
                        <dd>
                          {card.lease.executor}（{card.lease.mode}）剩余 {liveSecondsLeft(card)} 秒，到期 {formatMoment(card.lease.expires_at)}
                        </dd>
                      </div>
                    )}
                    {card.frozen && <div><dt>冻结原因</dt><dd>{card.frozen_by}：{card.freeze_reason || '—'}</dd></div>}
                    {card.archive_reason && <div><dt>转存原因</dt><dd>{card.archive_reason}</dd></div>}
                    <div><dt>状态说明</dt><dd>{card.reason || '—'}</dd></div>
                  </dl>

                  {card.lease_history.length > 0 && (
                    <details className="replay-sub">
                      <summary>租约历史（{card.lease_history.length}）</summary>
                      {card.lease_history.map((history, index) => (
                        <p key={`${history.lease_id}-${index}`}>
                          {history.executor} / {history.reason} / {formatMoment(history.closed_at)}
                          {history.reverted_stages.length > 0 && `，回收：${history.reverted_stages.join('、')}`}
                          {history.sealed_stages.length > 0 && `，封存：${history.sealed_stages.join('、')}`}
                        </p>
                      ))}
                    </details>
                  )}

                  <div className="replay-actions">
                    {card.status === '已冻结' ? (
                      <button
                        className="btn primary"
                        type="button"
                        onClick={() => void runAction({ card_id: card.id, action: '解除冻结', operator: executor }, '卡片已解除冻结')}
                      >
                        解除冻结
                      </button>
                    ) : (
                      <button
                        className="btn"
                        type="button"
                        disabled={card.status !== '待领取'}
                        onClick={() => void runAction(
                          { card_id: card.id, action: '冻结卡片', reason: freezeReason[card.id] ?? '', operator: executor },
                          '卡片已冻结，执行者暂时领不到',
                        )}
                      >
                        冻结卡片
                      </button>
                    )}
                    <input
                      className="inline-input"
                      value={freezeReason[card.id] ?? ''}
                      placeholder="冻结原因（可选）"
                      onChange={(event) => setFreezeReason((prev) => ({ ...prev, [card.id]: event.target.value }))}
                    />
                    <button
                      className="btn primary"
                      type="button"
                      disabled={!card.claimable}
                      title={card.claimable ? '' : '未到期、次数耗尽、已冻结或作业已结束'}
                      onClick={() => void runAction(
                        { card_id: card.id, action: '领取尝试', executor, mode: '人工立即', lease_seconds: leaseSeconds },
                        '已发放本次尝试的租约',
                      )}
                    >
                      人工立即领取
                    </button>
                    {card.lease && (
                      <>
                        <select
                          className="inline-input"
                          value={outcomes}
                          onChange={(event) => setReceiptDraft((prev) => ({ ...prev, [`${card.id}:outcome`]: event.target.value }))}
                        >
                          <option value="成功">成功</option>
                          <option value="失败">失败</option>
                        </select>
                        <select
                          className="inline-input"
                          value={stageDraft}
                          onChange={(event) => setReceiptDraft((prev) => ({ ...prev, [`${card.id}:stage`]: event.target.value }))}
                        >
                          {card.lease.pending_stages.map((stage) => <option key={stage} value={stage}>{stage}</option>)}
                        </select>
                        <input
                          className="inline-input"
                          value={errorDraft}
                          placeholder="异常信息，如 HTTP 503"
                          onChange={(event) => setReceiptDraft((prev) => ({ ...prev, [`${card.id}:error`]: event.target.value }))}
                        />
                        <button
                          className="btn"
                          type="button"
                          onClick={() => void runAction(
                            {
                              card_id: card.id,
                              action: '上报结果',
                              executor: card.lease?.executor,
                              lease_id: card.lease?.id,
                              outcome: outcomes,
                              stage: stageDraft,
                              error: errorDraft,
                            },
                            '回执已记录',
                          )}
                        >
                          上报回执
                        </button>
                      </>
                    )}
                    {card.status === '待领取' && !due && (
                      <span className="replay-hint">等待承诺未到期，不能抢先领取</span>
                    )}
                  </div>
                </div>
              )}
            </article>
          )
        })}
        {cards.length === 0 && <p className="empty-state">暂无重放卡片，队列为空</p>}
      </div>

      {showLate && (
        <section className="late-panel">
          <h3>迟到证据</h3>
          <p className="page-desc">租约过期后才到达的执行回执，只留存证据，不改变卡片状态。</p>
          {lateReceipts.length === 0 ? (
            <p className="empty-state">暂无迟到回执</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr><th>卡片</th><th>租约</th><th>执行者</th><th>结果</th><th>阶段</th><th>异常</th><th>到达时刻</th><th>原到期时刻</th></tr>
              </thead>
              <tbody>
                {lateReceipts.map((receipt) => (
                  <tr key={receipt.id}>
                    <td>#{receipt.card_id}</td>
                    <td>{receipt.lease_id ?? '—'}</td>
                    <td>{receipt.executor}</td>
                    <td>{receipt.outcome}</td>
                    <td>{receipt.stage ?? '—'}</td>
                    <td>{receipt.error ?? '—'}</td>
                    <td>{formatMoment(receipt.received_at)}</td>
                    <td>{formatMoment(receipt.expected_expires_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 张重放卡片 · 迟到回执 {lateReceipts.length} 条</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </div>
  )
}
