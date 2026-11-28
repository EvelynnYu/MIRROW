import { getApiBase } from '../config';
import type { Essay } from '../components/EssayCard';

export type EssaySeries = { root: Essay; parts: Essay[]; latest: Essay };

// The API returns metadata newest first. Resolve families before paginating the directory.
export function groupEssays(items: Essay[]): EssaySeries[] {
  const byId = new Map(items.map(item => [item.id, item]));
  const groups = new Map<string, EssaySeries>();
  const roots = new Map<string, Essay>();
  for (const item of byId.values()) {
    let root = item;
    const path = new Set<string>();
    while (root.continued_from && !roots.has(root.id) && !path.has(root.id)) {
      path.add(root.id);
      const parent = byId.get(root.continued_from.id);
      if (!parent) break;
      root = parent;
    }
    root = roots.get(root.id) ?? root;
    for (const id of path) roots.set(id, root);
    roots.set(root.id, root);
    const group = groups.get(root.id);
    if (group) group.parts.push(item);
    else groups.set(root.id, { root, parts: [item], latest: item });
  }
  for (const group of groups.values()) {
    group.parts.reverse();
    group.parts = [group.root, ...group.parts.filter(part => part.id !== group.root.id)];
  }
  return [...groups.values()];
}

export async function loadEssayIndex(signal: AbortSignal, query = ''): Promise<Essay[]> {
  const items: Essay[] = [];
  while (true) {
    const params = new URLSearchParams({ query, offset: String(items.length), limit: '100' });
    const response = await fetch(`${getApiBase()}/api/wander/essays?${params}`, { signal });
    if (!response.ok) throw new Error('随笔目录暂时无法读取');
    const page: { items: Essay[]; total: number } = await response.json();
    items.push(...page.items);
    if (!page.items.length || items.length >= page.total) return items;
  }
}
