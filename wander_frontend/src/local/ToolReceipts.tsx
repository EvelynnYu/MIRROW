export default function ToolReceipts({value}: {value?: string}) {
  let records: Array<{event_id?:string;tool?:string;status?:string;completed_at?:string;result?:unknown}> = [];
  try { const parsed = JSON.parse(value || '[]'); if (Array.isArray(parsed)) records = parsed; } catch { /* legacy row */ }
  if (!records.length) return null;
  return <details><summary>工具记录 · {records.length}</summary>{records.map((item,index) => <div key={item.event_id || index}>
    <small>{item.tool} · {item.status || '结果未知'} · {item.completed_at && new Date(item.completed_at).toLocaleString()}</small>
    <p>{typeof item.result === 'string' ? item.result : JSON.stringify(item.result)}</p>
  </div>)}</details>;
}
