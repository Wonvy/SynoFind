import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { FileIcon as FileTypeIcon, defaultStyles, type FileIconProps } from 'react-file-icon';
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Copy, Download, FolderOpen, Grid2X2, List, LoaderCircle, Search, SlidersHorizontal, X } from 'lucide-react';
import { api, copyText, downloadFile, fileURL, isAbort, messageOf } from './api';
import { EMPTY_FILTERS, formatBytes, formatDate, searchQuery, type SearchFilters } from './format';
import { Preview } from './Preview';
import type { FileItem, IndexStatus, Notify, SearchResult } from './types';

type PathStyle = 'windows' | 'dsm';
type ViewMode = 'list' | 'grid';
type ResultMode = 'paged' | 'scroll';

function savedChoice<T extends string>(key: string, fallback: T, choices: T[]): T {
  try {
    const value = localStorage.getItem(key);
    return choices.includes(value as T) ? value as T : fallback;
  } catch { return fallback; }
}

export function FileIcon({ file, large = false, hoverPreview = false }: { file: FileItem; large?: boolean; hoverPreview?: boolean }) {
  const [showPreview, setShowPreview] = useState(false);
  const ext = file.extension.toLowerCase();
  const style: Partial<FileIconProps> = defaultStyles[ext] ?? {};
  return <span
    className={'file-icon' + (large ? ' large' : '') + (file.preview_type === 'image' ? ' image' : '')}
    onMouseEnter={() => { if (hoverPreview && file.preview_type === 'image') setShowPreview(true); }}
    onMouseLeave={() => setShowPreview(false)}
    aria-hidden="true"
  >
    <FileTypeIcon extension={ext || undefined} color="#f1f4fa" glyphColor="#7f99bd" {...style} />
    {showPreview && <span className="thumbnail-popover"><img src={fileURL(file.id, true)} alt="" /></span>}
  </span>;
}

function GridArt({ file }: { file: FileItem }) {
  const [failed, setFailed] = useState(false);
  return <div className="file-card-art">
    {file.preview_type === 'image' && !failed
      ? <img src={fileURL(file.id, true)} alt="" loading="lazy" onError={() => setFailed(true)} />
      : <FileIcon file={file} large />}
  </div>;
}

function escapeRegex(value: string) {
  const special = new Set(['\\', '^', '$', '.', '|', '?', '*', '+', '(', ')', '[', ']', '{', '}']);
  return Array.from(value, character => special.has(character) ? '\\' + character : character).join('');
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const terms = useMemo(() => query.trim().replace(/[?*]/g, ' ').split(/\s+/).filter(Boolean), [query]);
  if (!terms.length) return <>{text}</>;
  const source = terms.map(escapeRegex).sort((a, b) => b.length - a.length).join('|');
  const chunks = text.split(new RegExp('(' + source + ')', 'ig'));
  const matcher = new RegExp('^(?:' + source + ')$', 'i');
  return <>{chunks.map((chunk, index) => matcher.test(chunk) ? <mark key={index}>{chunk}</mark> : chunk)}</>;
}

function fullPath(file: FileItem, style: PathStyle) {
  if (style === 'windows') return file.smb_path || file.path.replace(/\//g, '\\');
  return '/' + file.path.replace(/^\/+/, '');
}

function parentPath(value: string) {
  const clean = value.replace(/[\\/]+$/, '');
  const separator = Math.max(clean.lastIndexOf('/'), clean.lastIndexOf('\\'));
  return separator >= 0 ? clean.slice(0, separator) : '';
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
  const [pathStyle, setPathStyle] = useState<PathStyle>(() => savedChoice('synofind-path-style', 'windows', ['windows', 'dsm']));
  const [viewMode, setViewMode] = useState<ViewMode>(() => savedChoice('synofind-view-mode', 'list', ['list', 'grid']));
  const [resultMode, setResultMode] = useState<ResultMode>(() => savedChoice('synofind-result-mode', 'paged', ['paged', 'scroll']));
  const closePreview = useCallback(() => setPreview(null), []);
  const searchRef = useRef<HTMLInputElement>(null);
  const activeSearch = useRef<AbortController | null>(null);
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const wheelLock = useRef(false);
  const scrollLoadLock = useRef(false);
  const filterCount = Object.values(filters).filter(Boolean).length;
  const inputPending = input.trim() !== query.trim();
  const pages = Math.max(1, Math.ceil((result?.total ?? 0) / 50));

  useEffect(() => {
    const timer = setTimeout(() => { setQuery(input); setPage(1); }, 250);
    return () => clearTimeout(timer);
  }, [input]);
  useEffect(() => {
    try { localStorage.setItem('synofind-path-style', pathStyle); } catch { /* Preference is optional. */ }
  }, [pathStyle]);
  useEffect(() => {
    try { localStorage.setItem('synofind-view-mode', viewMode); } catch { /* Preference is optional. */ }
  }, [viewMode]);
  useEffect(() => {
    try { localStorage.setItem('synofind-result-mode', resultMode); } catch { /* Preference is optional. */ }
  }, [resultMode]);
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); searchRef.current?.focus(); }
    };
    window.addEventListener('keydown', focusSearch);
    return () => window.removeEventListener('keydown', focusSearch);
  }, []);
  useEffect(() => {
    if (inputPending) return;
    const controller = new AbortController();
    activeSearch.current = controller;
    setLoading(true);
    setError('');
    try {
      const params = searchQuery(query, filters, page, sort, order);
      api<SearchResult>('/search?' + params, { signal: controller.signal })
        .then(data => {
          if (controller.signal.aborted) return;
          if (page > 1 && data.total > 0 && data.items.length === 0) setPage(1);
          setResult(previous => resultMode === 'scroll' && page > 1
            ? { ...data, items: [...(previous?.items ?? []), ...data.items] }
            : data);
        })
        .catch(reason => {
          if (!controller.signal.aborted && !isAbort(reason)) { setError(messageOf(reason)); setResult(null); }
        })
        .finally(() => { if (!controller.signal.aborted) { setLoading(false); scrollLoadLock.current = false; } });
    } catch (reason) { setError(messageOf(reason)); setResult(null); setLoading(false); }
    return () => {
      controller.abort();
      scrollLoadLock.current = false;
      if (activeSearch.current === controller) activeSearch.current = null;
    };
  }, [query, input, inputPending, filters, page, sort, order, retry, status?.connected, status?.root_path, resultMode]);

  useEffect(() => {
    const target = loadMoreRef.current;
    if (!target || resultMode !== 'scroll' || loading || inputPending || page >= pages) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting) && !scrollLoadLock.current) {
        scrollLoadLock.current = true;
        setPage(value => value + 1);
      }
    }, { rootMargin: '260px' });
    observer.observe(target);
    return () => observer.disconnect();
  }, [resultMode, loading, inputPending, page, pages]);

  function changeSearch(value: string) {
    activeSearch.current?.abort();
    activeSearch.current = null;
    setLoading(true);
    setError('');
    setResult(null);
    setInput(value);
  }
  function applyFilters(event: FormEvent) {
    event.preventDefault();
    try { searchQuery(query, draft, 1, sort, order); setFilters({ ...draft }); setPage(1); setFiltersOpen(false); }
    catch (reason) { notify(messageOf(reason), 'error'); }
  }
  function changeSort(field: string) {
    if (sort === field) setOrder(value => value === 'asc' ? 'desc' : 'asc');
    else { setSort(field); setOrder(field === 'modified' ? 'desc' : 'asc'); }
    setPage(1);
  }
  async function copyPath(file: FileItem) {
    try { await copyText(fullPath(file, pathStyle)); notify('文件路径已复制。'); }
    catch (reason) { notify(messageOf(reason), 'error'); }
  }
  async function download(file: FileItem) {
    setDownloading(file.id);
    try { await downloadFile(file.id, file.name); }
    catch (reason) { notify(messageOf(reason), 'error'); }
    finally { setDownloading(''); }
  }
  function sortHeader(field: string, title: string) {
    return <button className={'sort-button' + (sort === field ? ' selected' : '')} onClick={() => changeSort(field)}>
      {title}{sort === field && (order === 'asc' ? <ArrowUp size={13} /> : <ArrowDown size={13} />)}
    </button>;
  }
  function pathLabel(file: FileItem) {
    const folder = parentPath(fullPath(file, pathStyle));
    return folder || (pathStyle === 'windows' && !file.smb_path ? '相对路径' : '根目录');
  }
  const items = result?.items ?? [];

  return <>
    <div className="page-heading search-heading">
      <div><span className="eyebrow">FIND SOMETHING GOOD</span><h1>你的文件，触手可及。</h1><p>通过群晖 File Station 搜索文件名和属性。</p></div>
      <div className="heading-actions">
        <span className="library-count"><DatabaseGlyph /><span><strong>群晖</strong><small>原生文件搜索</small></span></span>
      </div>
    </div>

    <section className="search-area" aria-label="搜索文件">
      <div className="search-input-wrap">
        <Search size={23} />
        <input ref={searchRef} value={input} onChange={event => changeSearch(event.target.value)} placeholder="搜索文件名…" aria-label="搜索文件名" autoComplete="off" />
        {input ? <button className="icon-button" onClick={() => { changeSearch(''); searchRef.current?.focus(); }} aria-label="清空搜索"><X size={17} /></button> : <kbd>Ctrl K</kbd>}
      </div>
      <div className="search-meta">
        <span><span className="tiny-dot" />{status?.root_path ? status.root_path.split(/[\\/]/).filter(Boolean).pop() : '文件库'}<span className="meta-divider">·</span>支持 DSM 文件名匹配规则</span>
        <button className={'filter-toggle' + (filterCount ? ' selected' : '')} onClick={() => { setDraft({ ...filters }); setFiltersOpen(value => !value); }} aria-expanded={filtersOpen}><SlidersHorizontal size={15} />高级筛选{filterCount > 0 && <span className="count-badge">{filterCount}</span>}</button>
      </div>
      {filtersOpen && <form className="filter-panel" onSubmit={applyFilters}>
        <div className="filter-panel-heading"><strong>高级搜索过滤器</strong><span>按文件类型、大小和修改日期缩小范围</span></div>
        <div className="filter-fields">
          <label>文件类型<input placeholder="例如 pdf, docx, jpg" value={draft.ext} onChange={event => setDraft({ ...draft, ext: event.target.value })} /></label>
          <label>最小大小（MB）<input type="number" step="any" min="0" value={draft.minSize} onChange={event => setDraft({ ...draft, minSize: event.target.value })} placeholder="不限" /></label>
          <label>最大大小（MB）<input type="number" step="any" min="0" value={draft.maxSize} onChange={event => setDraft({ ...draft, maxSize: event.target.value })} placeholder="不限" /></label>
          <label>修改日期起<input type="date" value={draft.after} onChange={event => setDraft({ ...draft, after: event.target.value })} /></label>
          <label>修改日期止<input type="date" value={draft.before} onChange={event => setDraft({ ...draft, before: event.target.value })} /></label>
        </div>
        <div className="filter-actions">
          <button type="button" className="button ghost" onClick={() => { setDraft({ ...EMPTY_FILTERS }); setFilters({ ...EMPTY_FILTERS }); setPage(1); }}>清除筛选</button>
          <button type="submit" className="button primary small">应用筛选</button>
        </div>
      </form>}
    </section>

    <section className="results-card" aria-label="搜索结果" aria-busy={loading || inputPending} onWheel={event => {
      if (resultMode !== 'paged' || Math.abs(event.deltaY) < 12 || wheelLock.current) return;
      const nextPage = page + (event.deltaY > 0 ? 1 : -1);
      if (nextPage < 1 || nextPage > pages || loading || inputPending) return;
      event.preventDefault();
      wheelLock.current = true;
      setPage(nextPage);
      window.setTimeout(() => { wheelLock.current = false; }, 450);
    }}>
      <div className="results-heading">
        <div className="results-title">
          <h2>{query.trim() || filterCount ? '搜索结果' : '文件名搜索'}</h2>
          {!loading && result && <span>{result.total.toLocaleString('zh-CN')} 项</span>}
          {(loading || inputPending) && <LoaderCircle className="spin" size={16} />}
        </div>
        <div className="results-heading-right">
          {filterCount > 0 && <button className="clear-filter" onClick={() => { setFilters({ ...EMPTY_FILTERS }); setDraft({ ...EMPTY_FILTERS }); setPage(1); }}>清除筛选 <X size={12} /></button>}
          <div className="display-controls">
            <div className="segmented-control result-mode-toggle" role="group" aria-label="加载方式">
              <button className={resultMode === 'scroll' ? 'active' : ''} onClick={() => { setResultMode('scroll'); setPage(1); }} aria-label="滚动视图" title="滚动视图" aria-pressed={resultMode === 'scroll'}><span className="result-mode-short">滚动</span></button>
              <button className={resultMode === 'paged' ? 'active' : ''} onClick={() => { setResultMode('paged'); setPage(1); }} aria-label="分页视图" title="分页视图（鼠标滚轮翻页）" aria-pressed={resultMode === 'paged'}><span className="result-mode-short">分页</span></button>
            </div>
            <div className="segmented-control path-toggle" role="group" aria-label="路径格式">
              <button className={pathStyle === 'windows' ? 'active' : ''} onClick={() => setPathStyle('windows')} aria-pressed={pathStyle === 'windows'}>Windows</button>
              <button className={pathStyle === 'dsm' ? 'active' : ''} onClick={() => setPathStyle('dsm')} aria-pressed={pathStyle === 'dsm'}>DSM</button>
            </div>
            <div className="segmented-control view-toggle" role="group" aria-label="结果视图">
              <button className={viewMode === 'list' ? 'active' : ''} onClick={() => setViewMode('list')} aria-label="列表视图" aria-pressed={viewMode === 'list'}><List size={15} /></button>
              <button className={viewMode === 'grid' ? 'active' : ''} onClick={() => setViewMode('grid')} aria-label="图标视图" aria-pressed={viewMode === 'grid'}><Grid2X2 size={14} /></button>
            </div>
          </div>
        </div>
      </div>
      {pathStyle === 'windows' && items.length > 0 && items.some(file => !file.smb_path) && <p className="path-hint">未配置 Windows 共享根路径的文件会显示相对路径；在设置中填写 SMB 路径可复制完整网络路径。</p>}
      {error ? <div className="empty-state"><FolderOpen size={40} /><h3>暂时无法获取文件</h3><p>{error}</p><button className="button secondary" onClick={() => setRetry(value => value + 1)}>重新加载</button></div>
        : !result && loading ? <div className="empty-state"><LoaderCircle size={30} className="spin" /><p>{inputPending ? '正在等待新搜索…' : '正在向群晖查询…'}</p></div>
          : result?.items.length === 0 ? <div className="empty-state"><span className="empty-icon"><Search size={30} /></span><h3>{query || filterCount ? '没有找到匹配的文件' : '输入文件名开始搜索'}</h3><p>{query || filterCount ? '试试更短的关键词，或调整筛选条件。' : '也可以使用文件类型、大小或日期筛选条件。'}</p>{!query && !filterCount && !status?.connected && <a className="button secondary" href="#settings">配置 DSM 连接</a>}</div>
            : viewMode === 'list' ? <div className="table-scroll">
              <table className={'file-table' + (loading || inputPending ? ' refreshing' : '')}>
                <thead><tr><th>{sortHeader('name', '文件名称')}</th><th className="type-column">类型</th><th className="size-column">{sortHeader('size', '大小')}</th><th className="date-column">{sortHeader('modified', '修改时间')}</th><th className="actions-column"><span className="sr-only">操作</span></th></tr></thead>
                <tbody>{items.map(file => <tr key={file.id}>
                  <td><div className="file-name-cell">
                    <FileIcon file={file} hoverPreview />
                    <div><button className="file-name" title={file.name} onClick={() => setPreview(file)}><HighlightedText text={file.name} query={query} /></button>
                      <button className="file-path-copy" title={'复制完整路径：' + fullPath(file, pathStyle)} onClick={() => void copyPath(file)}>{pathLabel(file)}</button>
                    </div>
                  </div></td>
                  <td className="type-column"><span className="extension">{file.extension ? file.extension.toUpperCase() : '文件'}</span></td>
                  <td className="size-column">{formatBytes(file.size)}</td><td className="date-column">{formatDate(file.modified_at)}</td>
                  <td className="actions-column"><div className="row-actions">
                    <button className="icon-button" aria-label={'复制 ' + file.name + ' 的路径'} title="复制路径" onClick={() => void copyPath(file)}><Copy size={16} /></button>
                    <a className="icon-button" aria-label={'下载 ' + file.name} aria-disabled={downloading === file.id} title="下载文件" href={fileURL(file.id)} download={file.name} onClick={event => { event.preventDefault(); if (downloading !== file.id) void download(file); }}>{downloading === file.id ? <LoaderCircle className="spin" size={17} /> : <Download size={17} />}</a>
                  </div></td>
                </tr>)}</tbody>
              </table>
            </div> : <div className={'file-grid' + (loading || inputPending ? ' refreshing' : '')}>
              {items.map(file => <article className="file-card" key={file.id}>
                <button className="file-card-open" onClick={() => setPreview(file)} aria-label={'预览 ' + file.name}>
                  <GridArt file={file} />
                  <span className="file-card-name"><HighlightedText text={file.name} query={query} /></span>
                  <span className="file-card-path" title={fullPath(file, pathStyle)}>{pathLabel(file)}</span>
                </button>
                <div className="file-card-meta"><span>{formatBytes(file.size)}</span><span>{formatDate(file.modified_at, false)}</span></div>
                <div className="file-card-actions">
                  <button className="icon-button" aria-label={'复制 ' + file.name + ' 的路径'} title="复制路径" onClick={() => void copyPath(file)}><Copy size={15} /></button>
                  <a className="icon-button" aria-label={'下载 ' + file.name} title="下载文件" href={fileURL(file.id)} download={file.name} onClick={event => { event.preventDefault(); void download(file); }}><Download size={15} /></a>
                </div>
              </article>)}
            </div>}
      {result && result.total > 0 && (resultMode === 'paged' ? <div className="pagination">
        <span>第 {(page - 1) * 50 + 1}–{Math.min(page * 50, result.total)} 项，共 {result.total.toLocaleString('zh-CN')} 项</span>
        <div><button className="icon-button" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => setPage(value => value - 1)}><ChevronLeft size={18} /></button><span>{page} <span className="muted">/ {pages}</span></span><button className="icon-button" aria-label="下一页" disabled={page >= pages || loading} onClick={() => setPage(value => value + 1)}><ChevronRight size={18} /></button></div>
      </div> : <div className="scroll-load-status" ref={loadMoreRef} aria-live="polite">{loading ? <><LoaderCircle size={15} className="spin" />正在加载更多文件…</> : page < pages ? '继续向下滚动以加载更多' : `已显示全部 ${result.total.toLocaleString('zh-CN')} 项`}</div>)}
    </section>
    <div className="search-footnote"><span><span className={'status-dot' + (status?.connected ? '' : ' pending')} />{status?.connected ? status.last_search_at ? '最近连接 ' + formatDate(status.last_search_at) : 'DSM 已连接' : status?.last_error || '等待首次搜索'}</span><span>只匹配文件名，不搜索父目录和正文</span></div>
    {preview && <Preview file={preview} onClose={closePreview} notify={notify} />}
  </>;
}

function DatabaseGlyph() {
  return <svg width="27" height="30" viewBox="0 0 27 30" fill="none" aria-hidden="true"><ellipse cx="13.5" cy="7" rx="10" ry="4" stroke="currentColor" strokeWidth="1.6" /><path d="M3.5 7v8c0 2.2 4.5 4 10 4s10-1.8 10-4V7M3.5 15v8c0 2.2 4.5 4 10 4s10-1.8 10-4v-8" stroke="currentColor" strokeWidth="1.6" /></svg>;
}
