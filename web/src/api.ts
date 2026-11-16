let csrfToken = '';
export const API_BASE = '/api/v1';

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code = '') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export function setCsrfToken(token: string) { csrfToken = token; }
export function isAbort(error: unknown) { return error instanceof DOMException && error.name === 'AbortError'; }
export function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof TypeError) return '暂时无法连接 SynoFind，请检查网络后重试。';
  return error instanceof Error ? error.message : '操作未完成，请重试。';
}

async function checkedResponse(path: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers);
  const method = options.method ?? 'GET';
  if (options.body) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD'].includes(method) && csrfToken) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(API_BASE + path, { ...options, method, headers, credentials: 'same-origin' });
  if (!response.ok) {
    let code = '';
    let message = response.status === 401 ? '登录已过期，请重新登录。' : `请求未完成（${response.status}）`;
    try {
      const data = await response.json();
      code = data.error?.code ?? '';
      message = data.error?.message ?? message;
    } catch { /* Keep the HTTP error if the server did not return JSON. */ }
    if (response.status === 401 && path !== '/auth/login' && path !== '/auth/session') {
      setCsrfToken('');
      window.dispatchEvent(new Event('synofind:unauthorized'));
    }
    throw new ApiError(message, response.status, code);
  }
  return response;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await checkedResponse(path, options);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function previewText(id: string, signal: AbortSignal): Promise<string> {
  const response = await checkedResponse(`/files/${encodeURIComponent(id)}/preview`, { signal });
  return response.text();
}

export async function previewBytes(id: string, signal: AbortSignal): Promise<ArrayBuffer> {
  const response = await checkedResponse(`/files/${encodeURIComponent(id)}/preview`, { signal });
  const blob = await response.blob();
  if (blob.size > 128 * 1024 * 1024) throw new Error('文件超过 128 MB，浏览器预览可能占用过多内存，请下载后查看。');
  return blob.arrayBuffer();
}

export function fileURL(id: string, preview = false) {
  return `${API_BASE}/files/${encodeURIComponent(id)}/${preview ? 'preview' : 'content'}`;
}

// Check current permissions and existence before handing a streaming download
// to the browser. This keeps expired sessions and missing-file errors in the UI.
export async function downloadFile(id: string, name: string): Promise<void> {
  await api(`/files/${encodeURIComponent(id)}`);
  const link = document.createElement('a');
  link.href = fileURL(id);
  link.download = name;
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* HTTP/permission fallback below. */ }
  }
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.style.position = 'fixed';
  field.style.left = '-10000px';
  document.body.appendChild(field);
  field.select();
  let copied = false;
  try { copied = document.execCommand('copy'); } finally { field.remove(); focused?.focus(); }
  if (!copied) throw new Error('浏览器未允许复制，请选中文字后手动复制。');
}
