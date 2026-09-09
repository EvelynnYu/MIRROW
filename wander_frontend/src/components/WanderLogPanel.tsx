import { useWanderLog } from '../hooks/useWanderLog';
import { useModalFocus } from '../hooks/useModalFocus';

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null) return '—';
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}秒`;
  return `${Math.floor(total / 60)}分${String(total % 60).padStart(2, '0')}秒`;
}

function runKey(log: any) {
  return String(log.details?.run_id || log.run_id || log.event_id || log.timestamp || 'unknown-run');
}

function groupByRun(logs: any[]) {
  const groups: Array<{ key: string; logs: any[] }> = [];
  const indexes = new Map<string, number>();
  logs.forEach(log => {
    const key = runKey(log);
    const existing = indexes.get(key);
    if (existing == null) {
      indexes.set(key, groups.length);
      groups.push({ key, logs: [log] });
    } else {
      groups[existing].logs.push(log);
    }
  });
  return groups;
}

function activityStatus(log: any) {
  const projected = String(log.projected_status || log.details?.projected_status || '');
  const state = String(log.state || log.details?.state || '');
  if (projected === 'not_started') {
    const reason = String(log.abort_reason || log.details?.abort_reason || '');
    return reason === 'not_started_after_user_interrupt'
      ? { label: '未执行·本轮被打断', icon: '⏭️', className: 'not-started' }
      : { label: '未执行·前序活动失败', icon: '⏭️', className: 'not-started' };
  }
  if (state === 'interrupted') return { label: '中断', icon: '⏸️', className: 'interrupted' };
  if (state === 'aborted' || (log.nodes || []).some((node: any) => node.status === 'failed')) {
    return { label: '失败', icon: '⚠️', className: 'failed' };
  }
  if (state === 'completed') return { label: '完成', icon: '✅', className: 'completed' };
  return { label: '进行中', icon: '⏳', className: 'running' };
}

function shareStatus(log: any) {
  const share = log.judgment_result?.share;
  const delivery = String(log.details?.delivery_status || 'not_created');
  if (delivery === 'sent') return { label: '已分享给用户', icon: '📨', className: 'sent' };
  if (share === false || delivery === 'not_requested' || delivery === 'not_created') {
    return { label: '留在心里 · 未分享', icon: '🫧', className: 'not-shared' };
  }
  if (delivery === 'sending') return { label: '想分享 · 发送中', icon: '📤', className: 'sending' };
  if (delivery === 'suppressed') return { label: '想分享 · 勿扰中未发送', icon: '🔕', className: 'not-shared' };
  if (delivery === 'failed') return { label: '想分享 · 分享失败', icon: '⚠️', className: 'share-failed' };
  if (share === true) return { label: '想分享 · 未送达', icon: '🕊️', className: 'deferred' };
  return { label: '分享状态未确认', icon: '？', className: 'unknown' };
}

function gapSeconds(previous: any, next: any) {
  if (!previous?.completed_at || !next?.started_at) return null;
  const gap = (new Date(next.started_at).getTime() - new Date(previous.completed_at).getTime()) / 1000;
  return Number.isFinite(gap) && gap > 1 ? gap : null;
}

function wakeReasonLabel(reason: string) {
  const labels: Record<string, string> = {
    model_wait: '自主决定的休息', fallback_wait: '未取得有效等待时长 · 默认休息',
    continue_next: '延续刚才的想法', natural_success: '自然休息后再计划', natural_skipped: '当前材料冷却',
    execution_error_backoff: '执行失败退避', plan_cancelled: '计划被取消后重试', plan_error: '计划器异常后重试', plan_failed: '计划未形成后重试',
  };
  return labels[reason] || reason || '等待下一次计划';
}

const typeIcons: Record<string, string> = {
  keyword_expansion: '🔤', memory_fetch: '🧠', user_tracking: '📷', browse_news: '📰',
  browse_xiaohongshu: '📕', self_reflection: '🤔', browse_bookmarks: '⭐', sleep: '😴',
  host_group_activity: '💬', listen_music: '🎵', browse_taobao: '🛍️', browse_social_feed: '💌',
};

function activityOrder(log: any, fallback: number) {
  const value = Number(log.details?.activity_order ?? log.activity_order);
  return Number.isFinite(value) ? value + 1 : fallback;
}

function ActivityLog({ log, fallbackOrder }: { log: any; fallbackOrder: number }) {
  const status = activityStatus(log);
  const share = shareStatus(log);
  const nodes = Array.isArray(log.nodes) ? log.nodes : (Array.isArray(log.details?.nodes) ? log.details.nodes : []);
  const created = log.created_at || log.timestamp;
  const ended = log.ended_at;
  const executionDuration = log.execution_duration_seconds ?? log.details?.execution_duration_seconds
    ?? nodes.reduce((total: number, node: any) => total + (Number(node.duration_seconds) || 0), 0);
  const idleSchedule = log.details?.idle_schedule_seconds;
  const abortReason = String(log.abort_reason || log.details?.abort_reason || '');

  return (
    <div className={`ledger-item wander-log-item ${status.className}`}>
      <span className="ledger-type-label">{typeIcons[log.event_type] || '📌'}</span>
      <span className="wander-log-activity-order">活动 {activityOrder(log, fallbackOrder)}</span>
      <span className="ledger-content">
        <span>{log.description || log.event_type}</span>
        <span className="wander-log-status">{status.icon} {status.label}</span>
        <span className={`wander-log-share ${share.className}`}>{share.icon} {share.label}</span>
      </span>
      <span className="ledger-date">
        {created ? new Date(created).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}
        <small>{ended ? ` → ${new Date(ended).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : ' → 进行中'}</small>
      </span>
      <details className="wander-log-detail">
        <summary>查看活动与节点</summary>
        <div className="wander-log-timing">
          节点执行合计：{formatDuration(executionDuration)} · 活动生命周期：{formatDuration(log.duration_seconds)} · 节点数：{nodes.length}
          <small>（生命周期包含节点之间等待，不计入执行合计）</small>
        </div>
        {idleSchedule != null && idleSchedule > 1 && <div className="wander-log-gap">🕰️ 空闲调度：上一 run 结束到本 run 创建，间隔 {formatDuration(idleSchedule)}</div>}
        {log.details?.next_plan_at && <div className="wander-log-gap">⏰ 下次计划：{new Date(log.details.next_plan_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} · {wakeReasonLabel(String(log.details.wake_reason || ''))}</div>}
        {abortReason && status.className !== 'not-started' && <div className="wander-log-error">错误：{abortReason}</div>}
        {status.className === 'not-started' && <div className="wander-log-not-started">{status.label}</div>}
        {nodes.length > 0 && <div className="wander-log-nodes">{nodes.map((node: any, index: number) => {
          const gap = index > 0 ? gapSeconds(nodes[index - 1], node) : null;
          return (
            <div key={`${log.event_id}-${node.round_index || index}`}>
              {gap != null && <div className="wander-log-gap">⏳ 等待下一节点：{formatDuration(gap)}</div>}
              <div className="wander-log-node">
                <strong>节点 {node.round_index || index + 1} · {node.status || node.state || '未知'}</strong>
                <span>{node.started_at ? new Date(node.started_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '未开始'}{node.completed_at ? ` → ${new Date(node.completed_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : ''} · {formatDuration(node.duration_seconds)}</span>
                <div className="wander-log-node-source"><b>来源</b>：{node.source_summary || '暂无来源摘要'}</div>
                <div className="wander-log-node-reflection"><b>反思</b>：{node.reflection || '暂无节点反思'}</div>
                {(node.error || node.status === 'failed') && <em>错误：{node.error || '节点执行失败'}</em>}
              </div>
            </div>
          );
        })}</div>}
        {!nodes.length && log.process_log && <pre className="wander-log-process">{log.process_log}</pre>}
      </details>
    </div>
  );
}

function WanderLogPanel({ onClose, onOpenWishBoard }: { onClose: () => void; onOpenWishBoard?: () => void }) {
  useModalFocus(true, '漫想日志');
  const { wanderLogs, wanderLogFilter, setWanderLogFilter, wanderLogDate, setWanderLogDate, wanderLogStats, loading, error, fetchWanderLogs } = useWanderLog();
  const groups = groupByRun(wanderLogs);

  return (
    <div className="ledger-overlay" onClick={onClose}>
      <div className="ledger-modal wander-log-modal" role="dialog" aria-modal="true" aria-label="漫想日志" onClick={e => e.stopPropagation()}>
        <div className="ledger-title">
          <div className="wander-log-title-row">
            <span className="wander-log-heading">💭 漫想日志 {wanderLogStats.new_since_last_viewed ? `(+${wanderLogStats.new_since_last_viewed}新增) ` : ''}({wanderLogs.length}条)</span>
            {onOpenWishBoard && <button className="ledger-close-btn wander-wish-board-link" onClick={onOpenWishBoard} title="打开许愿板" aria-label="打开许愿板">🌠</button>}
            <button className="ledger-close-btn wander-log-close" onClick={onClose} title="关闭漫想日志" aria-label="关闭漫想日志">✕</button>
          </div>
          <div className="wander-log-filters">
            <input type="date" value={wanderLogDate} onChange={e => setWanderLogDate(e.target.value)} />
            <select value={wanderLogFilter} onChange={e => setWanderLogFilter(e.target.value as any)}>
              <option value="all">全部</option><option value="pushed">已分享</option><option value="discarded">未分享或未送达</option>
            </select>
            <button onClick={fetchWanderLogs} disabled={loading}>刷新</button>
          </div>
        </div>
        {error && <div className="public-status" role="alert">{error}</div>}
        {wanderLogs.length === 0 ? <div className="ledger-empty">{loading ? '正在读取本地记录…' : error ? '后端未连接或暂不可用' : '所选日期暂无漫想记录'}</div> : (
          <div className="ledger-list">
            {groups.map(group => (
              <section className="wander-log-run-group" key={group.key}>
                <div className="wander-log-run-heading">本次漫想 · {group.logs.length}个活动</div>
                {group.logs.map((log, index) => <ActivityLog key={log.event_id || `${group.key}-${index}`} log={log} fallbackOrder={index + 1} />)}
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default WanderLogPanel;
