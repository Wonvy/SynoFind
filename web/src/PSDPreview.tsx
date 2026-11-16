import { useEffect, useState } from 'react';
import { FileQuestion, LoaderCircle } from 'lucide-react';
import { getCompositeCanvas, readPsd } from 'ag-psd';
import { isAbort, previewBytes } from './api';

export function PSDPreview({ id, name }: { id: string; name: string }) {
  const [src, setSrc] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    let objectURL = '';
    setSrc(''); setError(''); setLoading(true);
    void previewBytes(id, controller.signal).then(buffer => {
      const psd = readPsd(buffer, { skipLayerImageData: true, skipThumbnail: true, totalMemoryLimit: 512 * 1024 * 1024 });
      const canvas = psd.canvas ?? getCompositeCanvas(psd);
      if (!canvas) throw new Error('该 PSD 没有可用的合成预览图，或使用了当前浏览器预览器不支持的颜色模式。');
      canvas.toBlob(blob => {
        if (controller.signal.aborted) return;
        if (!blob) { setError('无法生成 PSD 预览图。'); setLoading(false); return; }
        objectURL = URL.createObjectURL(blob);
        setSrc(objectURL); setLoading(false);
      }, 'image/png');
    }).catch(reason => {
      if (!isAbort(reason)) setError(reason instanceof Error ? reason.message : '无法读取此 PSD 文件。');
      setLoading(false);
    });
    return () => { controller.abort(); if (objectURL) URL.revokeObjectURL(objectURL); };
  }, [id]);
  if (loading) return <div className="empty-state"><LoaderCircle className="spin" size={28} /><p>正在解析 PSD 图层预览…</p></div>;
  if (error) return <div className="empty-state"><FileQuestion size={38} /><h3>无法预览此 PSD 文件</h3><p>{error}</p></div>;
  return <div className="psd-preview"><img src={src} alt={name} /></div>;
}
