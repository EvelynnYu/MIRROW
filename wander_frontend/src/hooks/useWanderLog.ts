import { useState, useRef, useEffect } from 'react';
import { getApiBase } from '../config';

export function useWanderLog() {
  const today = new Date();
  const [wanderLogs, setWanderLogs] = useState<any[]>([]);
  const [wanderLogFilter, setWanderLogFilter] = useState<'all' | 'pushed' | 'discarded'>('all');
  const [wanderLogDate, setWanderLogDate] = useState(`${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`);
  const [wanderLogStats, setWanderLogStats] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const viewed = useRef('');

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    setWanderLogs([]); setWanderLogStats({});
    async function load() {
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 12000);
      setLoading(true);
      try {
        const query = new URLSearchParams({ limit: '0', date: wanderLogDate });
        if (wanderLogFilter !== 'all') query.set('pushed', String(wanderLogFilter === 'pushed'));
        if (viewed.current) query.set('last_viewed_at', viewed.current);
        const response = await fetch(`${getApiBase()}/api/wander/logs?${query}`, { signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!Array.isArray(data.logs)) throw new Error('接口未返回有效日志');
        if (!disposed) {
          setWanderLogs(data.logs); setWanderLogStats(data.stats || {}); setError('');
          viewed.current = new Date().toISOString();
        }
      } catch {
        if (!disposed) setError('未能刷新日志，请检查本地 API。下方如有记录，仅为上次成功读取的快照。');
      } finally {
        window.clearTimeout(timeout);
        if (!disposed) { setLoading(false); timer = window.setTimeout(load, 10000); }
      }
    }
    void load();
    return () => { disposed = true; controller?.abort(); window.clearTimeout(timer); };
  }, [wanderLogFilter, wanderLogDate, revision]);

  return { wanderLogs, wanderLogFilter, setWanderLogFilter, wanderLogDate, setWanderLogDate, wanderLogStats,
    loading, error, fetchWanderLogs: () => setRevision(value => value + 1) };
}
