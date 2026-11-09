export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index++; }
  return `${size.toLocaleString('zh-CN', { maximumFractionDigits: size < 10 ? 1 : 0 })} ${units[index]}`;
}

export function formatDate(date: string, withTime = true): string {
  if (!date) return '尚未记录';
  const value = new Date(date);
  if (Number.isNaN(value.getTime())) return '—';
  return value.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    ...(withTime ? { hour: '2-digit', minute: '2-digit', hour12: false } as const : {}),
  });
}

export interface SearchFilters {
  ext: string;
  minSize: string;
  maxSize: string;
  after: string;
  before: string;
}
export const EMPTY_FILTERS: SearchFilters = { ext: '', minSize: '', maxSize: '', after: '', before: '' };

export function searchQuery(query: string, filters: SearchFilters, page: number, sort: string, order: string) {
  const params = new URLSearchParams({ q: query.trim(), page: String(page), limit: '50', sort, order });
  const extensions = filters.ext.split(',').map(value => value.trim().replace(/^\./, '')).filter(Boolean).join(',');
  if (extensions) params.set('ext', extensions);
  for (const [name, value] of [['min_size', filters.minSize], ['max_size', filters.maxSize]]) {
    if (value.trim()) {
      const number = Number(value);
      if (!Number.isFinite(number) || number < 0) throw new Error('文件大小必须是大于或等于 0 的数字。');
      params.set(name, String(Math.round(number * 1024 * 1024)));
    }
  }
  if (filters.minSize && filters.maxSize && Number(filters.minSize) > Number(filters.maxSize)) {
    throw new Error('最小文件大小不能大于最大文件大小。');
  }
  if (filters.after) {
    const value = new Date(`${filters.after}T00:00:00`);
    if (Number.isNaN(value.getTime())) throw new Error('请选择有效的开始日期。');
    params.set('modified_after', value.toISOString());
  }
  if (filters.before) {
    const value = new Date(`${filters.before}T23:59:59.999`);
    if (Number.isNaN(value.getTime())) throw new Error('请选择有效的结束日期。');
    params.set('modified_before', value.toISOString());
  }
  if (filters.after && filters.before && filters.after > filters.before) throw new Error('开始日期不能晚于结束日期。');
  return params.toString();
}
