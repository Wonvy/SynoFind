import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Copy, Download, File, FileImage, FileMusic, FileText, FileVideo, FolderOpen, ListFilter, LoaderCircle, Search, SlidersHorizontal, X } from 'lucide-react';
import { api, copyText, downloadFile, fileURL, isAbort, messageOf } from './api';
import { EMPTY_FILTERS, formatBytes, formatDate, searchQuery, type SearchFilters } from './format';
import { Preview } from './Preview';
import type { FileItem, IndexStatus, Notify, SearchResult } from './types';

export function FileIcon({ file }: { file: FileItem }) {
  const ext = file.extension.toLowerCase();
  if (file.preview_type === 'image') return <span className="file-icon image"><FileImage size={20} /></span>;
  if (file.preview_type === 'pdf') return <span className="file-icon pdf"><FileText size={20} /></span>;
  if (['mp4', 'mkv', 'mov', 'avi', 'webm'].includes(ext)) return <span className="file-icon video"><FileVideo size={20} /></span>;
  if (['mp3', 'wav', 'flac', 'm4a', 'ogg'].includes(ext)) return <span className="file-icon music"><FileMusic size={20} /></span>;
  return <span className="file-icon"><File size={20} /></span>;
}

export function SearchPage({ status, notify }: { status: IndexStatus | null; notify: Notify }) {
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  const [filters, setFilters] = useState<SearchFilters>({ ...EMPTY_FILTERS });
  const [draft, setDraft] = useState<SearchFilters>({ ...EMPTY_FILTERS });
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState('name');
  const [order, setOrder] = useState('asc');
  const [result, setResult] = useState<SearchResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [downloading, setDownloading] = useState('');
  const [preview, setPreview] = useState<FileItem | null>(null);
  const closePreview = useCallback(() => setPreview(null), []);
  const searchRef = useRef<HTMLInputElement>(null);
  const filterCount = Object.values(filters).filter(Boolean).length;
  useEffect(() => {
    const timer = setTimeout(() => { setQuery(input); setPage(1); }, 250);
    return () => clearTimeout(timer);
  }, [input]);
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); searchRef.current?.focus(); }
    };
    window.addEventListener('keydown', focusSearch);
    return () => window.removeEventListener('keydown', focusSearch);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    try {
      const params = searchQuery(query, filters, page, sort, order);
      api<SearchResult>(`/search?${params}`, { signal: controller.signal })
        .then(data => { if (page > 1 && data.total > 0 && data.items.length === 0) setPage(1); setResult(data); })
        .catch(reason => { if (!isAbort(reason)) { setError(messageOf(reason)); setResult(null); } })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    } catch (reason) { setError(messageOf(reason)); setResult(null); setLoading(false); }
    return () => controller.abort();
  }, [query, filters, page, sort, order, retry, status?.last_completed_at, status?.root_path]);
  function applyFilters(event: FormEvent) {
    event.preventDefault();
    try { searchQuery(query, draft, 1, sort, order); setFilters({ ...draft }); setPage(1); setFiltersOpen(false); }
    catch (reason) { notify(messageOf(reason), 'error'); }
  }
  function changeSort(field: string) { if (sort === field) setOrder(value => value === 'asc' ? 'desc' : 'asc'); else { setSort(field); setOrder(field === 'modified' ? 'desc' : 'asc'); } setPage(1); }
  async function copyPath(file: FileItem) { try { await copyText(file.smb_path); notify('共享路径已复制，可粘贴到 Windows 文件资源管理器。'); } catch (reason) { notify(messageOf(reason), 'error'); } }
  async function download(file: FileItem) { setDownloading(file.id); try { await downloadFile(file.id, file.name); } catch (reason) { notify(messageOf(reason), 'error'); } finally { setDownloading(''); } }
  const pages = Math.max(1, Math.ceil((result?.total ?? 0) / 50));
  function sortHeader(field: string, title: string) { return <button className={`sort-button ${sort === field ? 'selected' : ''}`} onClick={() => changeSort(field)}>{title}{sort === field && (order === 'asc' ? <ArrowUp size={13} /> : <ArrowDown size={13} />)}</button>; }
  return <><div className="page-heading search-heading"><div><span className="eyebrow">FIND SOMETHING GOOD</span><h1>你的文件，触手可及。</h1><p>从文件名或路径开始，找到你需要的那一份。</p></div><span className="library-count"><DatabaseGlyph /><span><strong>{status ? status.total_files.toLocaleString('zh-CN') : '—'}</strong><small>个文件已索引</small></span></span></div><section className="search-area" aria-label="搜索文件"><div className="search-input-wrap"><Search size={23} /><input ref={searchRef} value={input} onChange={event => setInput(event.target.value)} placeholder="搜索文件名、文件夹路径…" aria-label="搜索文件名或路径" autoComplete="off" />{input ? <button className="icon-button" onClick={() => { setInput(''); searchRef.current?.focus(); }} aria-label="清空搜索"><X size={17} /></button> : <kbd>Ctrl K</kbd>}</div><div className="search-meta"><span><span className="tiny-dot" />{status?.root_path ? status.root_path.split(/[\\/]/).filter(Boolean).pop() : '文件库'}<span className="meta-divider">·</span>多个关键词用空格分隔</span><button className={`filter-toggle ${filterCount ? 'selected' : ''}`} onClick={() => { setDraft({ ...filters }); setFiltersOpen(value => !value); }} aria-expanded={filtersOpen}><SlidersHorizontal size={15} />筛选{filterCount > 0 && <span className="count-badge">{filterCount}</span>}</button></div>{filtersOpen && <form className="filter-panel" onSubmit={applyFilters}><div className="filter-fields"><label>文件类型<input placeholder="例如 pdf, docx, jpg" value={draft.ext} onChange={event => setDraft({ ...draft, ext: event.target.value })} /></label><label>最小大小（MB）<input type="number" step="any" min="0" value={draft.minSize} onChange={event => setDraft({ ...draft, minSize: event.target.value })} placeholder="不限" /></label><label>最大大小（MB）<input type="number" step="any" min="0" value={draft.maxSize} onChange={event => setDraft({ ...draft, maxSize: event.target.value })} placeholder="不限" /></label><label>修改日期起<input type="date" value={draft.after} onChange={event => setDraft({ ...draft, after: event.target.value })} /></label><label>修改日期止<input type="date" value={draft.before} onChange={event => setDraft({ ...draft, before: event.target.value })} /></label></div><div className="filter-actions"><button type="button" className="button ghost" onClick={() => { setDraft({ ...EMPTY_FILTERS }); setFilters({ ...EMPTY_FILTERS }); setPage(1); }}>清除筛选</button><button type="submit" className="button primary small">应用筛选</button></div></form>}</section><section className="results-card" aria-label="搜索结果" aria-busy={loading}><div className="results-heading"><div><h2>{query.trim() || filterCount ? '搜索结果' : '全部文件'}</h2>{!loading && result && <span>{result.total.toLocaleString('zh-CN')} 项</span>}{loading && <LoaderCircle className="spin" size={16} />}</div><div className="results-heading-right">{filterCount > 0 && <button className="clear-filter" onClick={() => { setFilters({ ...EMPTY_FILTERS }); setDraft({ ...EMPTY_FILTERS }); setPage(1); }}>清除筛选 <X size={12} /></button>}<ListFilter size={16} /></div></div>{error ? <div className="empty-state"><FolderOpen size={40} /><h3>暂时无法获取文件</h3><p>{error}</p><button className="button secondary" onClick={() => setRetry(value => value + 1)}>重新加载</button></div> : !result && loading ? <div className="empty-state"><LoaderCircle size={30} className="spin" /><p>正在查找文件…</p></div> : result?.items.length === 0 ? <div className="empty-state"><span className="empty-icon"><Search size={30} /></span><h3>{query || filterCount ? '没有找到匹配的文件' : status?.running ? '正在建立你的文件索引' : '文件库里还没有索引记录'}</h3><p>{query || filterCount ? '试试更短的关键词，或调整筛选条件。' : status?.running ? '首次扫描完成后，文件会显示在这里。' : '前往索引状态页，检查目录权限并刷新索引。'}</p>{!query && !filterCount && <a className="button secondary" href="#index">查看索引状态</a>}</div> : <div className="table-scroll"><table className={loading ? 'file-table refreshing' : 'file-table'}><thead><tr><th>{sortHeader('name', '文件名称')}</th><th className="type-column">类型</th><th className="size-column">{sortHeader('size', '大小')}</th><th className="date-column">{sortHeader('modified', '修改时间')}</th><th className="actions-column"><span className="sr-only">操作</span></th></tr></thead><tbody>{result?.items.map(file => <tr key={file.id}><td><div className="file-name-cell"><FileIcon file={file} /><div><button className="file-name" title={file.name} onClick={() => setPreview(file)}>{file.name}</button><span className="file-path" title={file.path}>{file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/')) : '根目录'}</span></div></div></td><td className="type-column"><span className="extension">{file.extension ? file.extension.toUpperCase() : '文件'}</span></td><td className="size-column">{formatBytes(file.size)}</td><td className="date-column">{formatDate(file.modified_at)}</td><td className="actions-column"><div className="row-actions"><button className="icon-button" aria-label={`复制 ${file.name} 的共享路径`} title="复制共享路径" onClick={() => void copyPath(file)} disabled={!file.smb_path}><Copy size={16} /></button><a className="icon-button" aria-label={`下载 ${file.name}`} aria-disabled={downloading === file.id} title="下载文件" href={fileURL(file.id)} download={file.name} onClick={event => { event.preventDefault(); if (downloading !== file.id) void download(file); }}>{downloading === file.id ? <LoaderCircle className="spin" size={17} /> : <Download size={17} />}</a></div></td></tr>)}</tbody></table></div>}{result && result.total > 0 && <div className="pagination"><span>第 {(page - 1) * 50 + 1}–{Math.min(page * 50, result.total)} 项，共 {result.total.toLocaleString('zh-CN')} 项</span><div><button className="icon-button" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => setPage(value => value - 1)}><ChevronLeft size={18} /></button><span>{page} <span className="muted">/ {pages}</span></span><button className="icon-button" aria-label="下一页" disabled={page >= pages || loading} onClick={() => setPage(value => value + 1)}><ChevronRight size={18} /></button></div></div>}</section><div className="search-footnote"><span><span className={`status-dot ${status?.running ? 'pending' : ''}`} />{status?.running ? '索引正在更新，结果将自动刷新' : status?.last_completed_at ? `索引更新于 ${formatDate(status.last_completed_at)}` : '等待首次索引完成'}</span><span>文件名与路径搜索</span></div>{preview && <Preview file={preview} onClose={closePreview} notify={notify} />}</>;
}

function DatabaseGlyph() { return <svg width="27" height="30" viewBox="0 0 27 30" fill="none" aria-hidden="true"><ellipse cx="13.5" cy="7" rx="10" ry="4" stroke="currentColor" strokeWidth="1.6" /><path d="M3.5 7v8c0 2.2 4.5 4 10 4s10-1.8 10-4V7M3.5 15v8c0 2.2 4.5 4 10 4s10-1.8 10-4v-8" stroke="currentColor" strokeWidth="1.6" /></svg>; }


