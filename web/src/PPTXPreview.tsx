import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, FileQuestion, LoaderCircle } from 'lucide-react';
import type { Viewer } from '@web-ppt/viewer-core';
import { isAbort, previewBytes } from './api';

export function PPTXPreview({ id, name }: { id: string; name: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Viewer | null>(null);
  const [slide, setSlide] = useState(0);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let viewer: Viewer | undefined;
    setLoading(true); setError(''); setCount(0); setSlide(0);
    void previewBytes(id, controller.signal).then(async bytes => {
      const [{ parse }, { Viewer: BrowserViewer }] = await Promise.all([import('@web-ppt/core'), import('@web-ppt/viewer-core')]);
      const presentation = await parse(bytes);
      if (controller.signal.aborted || !hostRef.current) return;
      viewer = new BrowserViewer(hostRef.current, presentation, { animate: false });
      viewer.setAnimate(false);
      viewer.onChange = index => setSlide(index);
      viewerRef.current = viewer;
      setCount(viewer.count); setSlide(viewer.index); setLoading(false);
    }).catch(reason => {
      if (!isAbort(reason)) setError(reason instanceof Error ? reason.message : '无法读取此 PowerPoint 文件。');
      setLoading(false);
    });
    return () => { controller.abort(); viewer?.destroy(); if (viewerRef.current === viewer) viewerRef.current = null; };
  }, [id]);
  return <div className="pptx-preview" aria-label={`${name} PowerPoint 预览`}>
    <div className="pptx-toolbar"><span>幻灯片预览</span><div><button className="icon-button" aria-label="上一张幻灯片" disabled={!viewerRef.current || slide <= 0} onClick={() => viewerRef.current?.prev()}><ChevronLeft size={18} /></button><span>{count ? `${slide + 1} / ${count}` : '— / —'}</span><button className="icon-button" aria-label="下一张幻灯片" disabled={!viewerRef.current || slide + 1 >= count} onClick={() => viewerRef.current?.next()}><ChevronRight size={18} /></button></div></div>
    <div className="pptx-stage" aria-busy={loading}><div className="pptx-slide-host" ref={hostRef} />{loading && <div className="empty-state"><LoaderCircle className="spin" size={28} /><p>正在解析演示文稿…</p></div>}{error && <div className="empty-state"><FileQuestion size={38} /><h3>无法预览此演示文稿</h3><p>{error}</p></div>}</div>
  </div>;
}
