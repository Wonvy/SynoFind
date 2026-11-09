import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, FileQuestion, LoaderCircle, RefreshCw } from 'lucide-react';
import { AnnotationMode, getDocument, GlobalWorkerOptions, type PDFDocumentLoadingTask, type PDFDocumentProxy, type PDFPageProxy, type RenderTask } from 'pdfjs-dist';
import workerURL from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { api, fileURL, isAbort } from './api';
import { fitPDFPage, pdfErrorMessage } from './pdf';

GlobalWorkerOptions.workerSrc = workerURL;

export default function PDFPreview({ id, name }: { id: string; name: string }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasHostRef = useRef<HTMLDivElement>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [width, setWidth] = useState(0);
  const [loading, setLoading] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const measure = () => setWidth(Math.max(1, Math.floor(element.clientWidth - 40)));
    const observer = new ResizeObserver(measure);
    observer.observe(element); measure();
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let task: PDFDocumentLoadingTask | undefined;
    let disposed = false;
    setDocument(null); setLoading(true); setError(''); setPage(1);
    const load = async () => {
      try {
        // Surface authentication and file permission errors before PDF.js owns
        // subsequent Range requests. No document bytes are fetched as a blob.
        await api(`/files/${encodeURIComponent(id)}`, { signal: controller.signal });
        if (disposed) return;
        task = getDocument({
          url: new URL(fileURL(id, true), window.location.origin).href,
          withCredentials: true,
          disableRange: false,
          disableStream: true,
          disableAutoFetch: true,
          rangeChunkSize: 256 * 1024,
          cMapUrl: `${window.location.origin}/pdfjs/cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${window.location.origin}/pdfjs/standard_fonts/`,
          iccUrl: `${window.location.origin}/pdfjs/iccs/`,
          wasmUrl: `${window.location.origin}/pdfjs/wasm/`,
          useWasm: false,
          disableFontFace: true,
          enableXfa: false,
          canvasMaxAreaInBytes: 32 * 1024 * 1024,
          maxImageSize: 32_000_000,
          verbosity: 0,
        });
        const loaded = await task.promise;
        if (!disposed) setDocument(loaded);
      } catch (reason) {
        if (!disposed && !isAbort(reason)) setError(pdfErrorMessage(reason));
      } finally { if (!disposed) setLoading(false); }
    };
    void load();
    return () => { disposed = true; controller.abort(); void task?.destroy().catch(() => {}); };
  }, [id, attempt]);

  useEffect(() => {
    const host = canvasHostRef.current;
    if (!document || !host || !width) return;
    let disposed = false;
    let renderTask: RenderTask | undefined;
    let pdfPage: PDFPageProxy | undefined;
    // Each render owns a fresh canvas: canceling a previous render cannot paint
    // into a newly selected page, and only one canvas remains attached.
    const canvas = window.document.createElement('canvas');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', `${name}，第 ${page} 页`);
    host.replaceChildren(canvas);
    viewportRef.current?.scrollTo({ top: 0 });
    setRendering(true); setError('');
    const render = async () => {
      try {
        pdfPage = await document.getPage(page);
        if (disposed) { pdfPage.cleanup(); return; }
        const natural = pdfPage.getViewport({ scale: 1 });
        const fitted = fitPDFPage(natural.width, natural.height, width, window.devicePixelRatio || 1);
        const viewport = pdfPage.getViewport({ scale: fitted.scale });
        canvas.width = fitted.pixelWidth;
        canvas.height = fitted.pixelHeight;
        canvas.style.width = `${fitted.width}px`;
        canvas.style.height = `${fitted.height}px`;
        renderTask = pdfPage.render({
          canvas,
          viewport,
          transform: fitted.outputScale === 1 ? undefined : [fitted.outputScale, 0, 0, fitted.outputScale, 0, 0],
          annotationMode: AnnotationMode.DISABLE,
          background: '#ffffff',
        });
        await renderTask.promise;
      } catch (reason) {
        if (!disposed && (reason as { name?: string })?.name !== 'RenderingCancelledException') setError(pdfErrorMessage(reason));
      } finally {
        pdfPage?.cleanup();
        if (!disposed) setRendering(false);
      }
    };
    void render();
    return () => {
      disposed = true; renderTask?.cancel();
      canvas.width = 0; canvas.height = 0; canvas.remove();
      pdfPage?.cleanup();
    };
  }, [document, page, width, name]);

  return <div className="pdf-viewer"><div className="pdf-toolbar"><span>PDF 预览<span className="pdf-fit-label">适应宽度</span></span><div><button className="icon-button" aria-label="PDF 上一页" disabled={!document || page <= 1 || loading || rendering} onClick={() => setPage(value => value - 1)}><ChevronLeft size={18} /></button><span className="pdf-page-count" aria-live="polite">{document ? `${page} / ${document.numPages}` : '— / —'}</span><button className="icon-button" aria-label="PDF 下一页" disabled={!document || page >= document.numPages || loading || rendering} onClick={() => setPage(value => value + 1)}><ChevronRight size={18} /></button></div></div><div className="pdf-viewport" ref={viewportRef} aria-busy={loading || rendering}><div className={`pdf-canvas-host ${error || loading ? 'pdf-hidden' : ''}`} ref={canvasHostRef} />{error ? <div className="empty-state pdf-message"><FileQuestion size={35} /><h3>无法预览此 PDF</h3><p>{error}</p><button className="button secondary small" onClick={() => setAttempt(value => value + 1)}><RefreshCw size={14} />重试</button></div> : (loading || rendering) && <div className="pdf-loading" role="status"><LoaderCircle size={21} className="spin" /><span>{loading ? '正在读取 PDF…' : `正在绘制第 ${page} 页…`}</span></div>}</div></div>;
}
