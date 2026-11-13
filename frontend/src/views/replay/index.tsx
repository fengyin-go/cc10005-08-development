import ReplayQueue from './ReplayQueue'

export default function ReplayView() {
  return (
    <section className="page" data-module="replay">
      <header className="page-head">
        <div>
          <h2>失败重放队列</h2>
          <p className="page-desc">
            按到期时间排列可领取卡片。操作员先冻结检查原因，再解除冻结让执行者领取；不提供无条件从头重跑。
            领取带有效期，执行者消失后只回收未确认的失败阶段，已确认成功的阶段保持封存。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open('/api/replay/export', '_blank')}>
            导出重放队列
          </button>
        </div>
      </header>
      <ReplayQueue />
    </section>
  )
}
