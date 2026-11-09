import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Copy, Download, FileQuestion, LoaderCircle, X } from 'lucide-react';
import { copyText, downloadFile, fileURL, isAbort, messageOf, previewText } from './api';
import { formatBytes, formatDate } from './format';
import type { FileItem, Notify } from './types';

const PDFPreview = lazy(() => import('./PDFPreview'));

export function Preview({ file, onClose, notify }: { file: FileItem; onClose: () => void; notify: Notify }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const [downloading, setDownloading] = useState(false);
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(file.preview_type === 'text');
  const [error, setError] = useState('');
  useEffect(() => {
    const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panelRef.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'Tab') {
        const elements = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], iframe, [tabindex="0"]') ?? []);
        if (!elements.length) { event.preventDefault(); return; }
        const first = elements[0]; const last = elements[elements.length - 1];
        if (event.shiftKey && (document.activeElement === first || document.activeElement === panelRef.current)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener('keydown', keydown);
    return () => { document.body.style.overflow = previousOverflow; window.removeEventListener('keydown', keydown); focused?.focus(); };
  }, [onClose]);
  useEffect(() => {
    const controller = new AbortController(); setError(''); setText('');
    if (file.preview_type === 'text') {
      setLoading(true);
      previewText(file.id, controller.signal).then(setText)
        .catch(reason => { if (!isAbort(reason)) setError(messageOf(reason)); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    } else setLoading(false);
    return () => controller.abort();
  }, [file.id, file.preview_type]);
  async function copyPath() { try { await copyText(file.smb_path); notify('共享路径已复制。'); } catch (reason) { notify(messageOf(reason), 'error'); } }
  async function download() { setDownloading(true); try { await downloadFile(file.id, file.name); } catch (reason) { notify(messageOf(reason), 'error'); } finally { setDownloading(false); } }
  return <div className="modal-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><div className="preview-dialog" role="dialog" aria-modal="true" aria-labelledby="preview-title" tabIndex={-1} ref={panelRef}><header className="preview-header"><div><h2 id="preview-title">{file.name}</h2><p title={file.path}>{file.path}</p></div><button className="icon-button" aria-label="关闭预览" onClick={onClose}><X size={21} /></button></header><div className={`preview-content ${file.preview_type || 'unsupported'}`}>{loading ? <div className="empty-state"><LoaderCircle className="spin" size={28} /><p>正在加载预览…</p></div> : error ? <div className="empty-state"><FileQuestion size={38} /><h3>无法预览此文件</h3><p>{error}</p><p>你仍可尝试下载文件。</p></div> : file.preview_type === 'text' ? <pre className="text-preview">{text || '（空文件）'}</pre> : file.preview_type === 'image' ? <img src={fileURL(file.id, true)} alt={file.name} onError={() => setError('图片可能已移动、没有读取权限，或格式不受浏览器支持。')} /> : file.preview_type === 'pdf' ? <Suspense fallback={<div className="empty-state"><LoaderCircle className="spin" size={28} /><p>正在准备 PDF 阅读器…</p></div>}><PDFPreview id={file.id} name={file.name} /></Suspense> : <div className="empty-state"><span className="empty-icon"><FileQuestion size={32} /></span><h3>此格式暂不支持在线预览</h3><p>下载后使用电脑上的应用打开。</p></div>}</div><footer className="preview-footer"><span>{formatBytes(file.size)}<span className="meta-divider">·</span>{formatDate(file.modified_at)}</span><div><button className="button secondary small" onClick={() => void copyPath()} disabled={!file.smb_path}><Copy size={15} />复制路径</button><button className="button primary small" disabled={downloading} onClick={() => void download()}>{downloading ? <LoaderCircle className="spin" size={16} /> : <Download size={16} />}{downloading ? '正在读取' : '下载文件'}</button></div></footer></div></div>;
}


