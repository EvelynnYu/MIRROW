/** Writes are never automatically retried: timeout means an unknown result. */
export async function apiFetch(input: string, init: RequestInit = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 15000);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) throw new Error('请求超时，结果未确认。请刷新核对后再操作。');
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}
