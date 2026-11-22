import { receiptStateLabels } from '../lib/wanderReceipt';

const labels: Record<string, string> = {
  status: '状态', result: '结果', error: '受阻原因', reason: '原因', summary: '记录',
  text: '内容', content: '内容', description: '说明', conclusion: '观察结论',
  steps: '步数', heart_rate: '心率', spo2: '血氧', battery: '电量',
  timestamp: '记录时间', observed_at: '观察时间', value: '读数', unit: '单位',
  data: '取得的数据', message: '说明', available: '可用', source: '来源',
};

// Only for display. Exact provider/history text stays in the separate audit view.
export default function ReadableReceiptValue({ value }: { value: unknown }) {
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text) return null;
    if (text.startsWith('{') || text.startsWith('[')) {
      let structured: unknown;
      try { structured = JSON.parse(text); } catch { /* ordinary prose */ }
      if (structured !== undefined) return <ReadableReceiptValue value={structured} />;
    }
    return <p style={{ whiteSpace: 'pre-wrap' }}>{receiptStateLabels[text] || text}</p>;
  }
  if (Array.isArray(value)) return <>{value.map((item, index) => <div key={index}><ReadableReceiptValue value={item} /></div>)}</>;
  if (value && typeof value === 'object') return <dl className="receipt-fields">{Object.entries(value).map(([key, item]) => {
    if (['node_id', 'source_node_id', 'activity_id', 'run_id', 'version', 'kind'].includes(key) || item == null || item === '') return null;
    return <div key={key}><dt>{labels[key] || key}</dt><dd><ReadableReceiptValue value={item} /></dd></div>;
  })}</dl>;
  return value == null ? null : <p>{typeof value === 'boolean' ? (value ? '是' : '否') : String(value)}</p>;
}
