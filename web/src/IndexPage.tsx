import { CheckCircle2, Clock3, Database, FolderOpen, Info, ShieldCheck, TriangleAlert } from 'lucide-react';
import { formatDate } from './format';
import type { IndexStatus } from './types';

export function IndexPage({ status, error }: { status: IndexStatus | null; error: string }) {
  const message = error || status?.last_error || '';
  return <>
    <div className="page-heading">
      <div><span className="eyebrow">NATIVE DSM SEARCH</span><h1>DSM 搜索连接</h1><p>搜索任务由群晖 File Station 执行，SynoFind 不扫描文件或维护索引。</p></div>
      <span className="subtle-badge">{message ? <TriangleAlert size={14} /> : <ShieldCheck size={14} />}{message ? '连接异常' : status?.connected ? '已连接' : '待连接'}</span>
    </div>
    <div className="stats-grid">
      <div className="stat-card"><span className="stat-icon"><Database size={21} /></span><span className="stat-label">搜索服务</span><strong className="text-stat">File Station.Search</strong><p>群晖官方异步搜索 API</p></div>
      <div className="stat-card"><span className="stat-icon green"><FolderOpen size={21} /></span><span className="stat-label">搜索范围</span><strong className="text-stat">{status?.root_path?.split('/').filter(Boolean).pop() || '未设置'}</strong><p>{status?.root_path || '前往设置选择共享文件夹'}</p></div>
      <div className="stat-card"><span className="stat-icon purple"><Clock3 size={21} /></span><span className="stat-label">最近搜索</span><strong className="date-stat">{status?.last_search_at ? formatDate(status.last_search_at) : '尚未搜索'}</strong><p>{status?.connected ? '群晖 API 已连接' : '首次搜索时建立 DSM 会话'}</p></div>
    </div>
    {message && <div className="inline-alert error" role="alert"><TriangleAlert size={17} /><span>{message}</span></div>}
    <section className="panel">
      <div className="panel-heading"><div><h2>文件名搜索</h2><p>使用 DSM 账户执行搜索和读取；共享文件夹权限仍由群晖决定。</p></div><span className="subtle-badge"><CheckCircle2 size={13} />只读</span></div>
      <div className="source-row"><span className="source-icon"><FolderOpen size={25} /></span><div><strong>{status?.root_path?.split('/').filter(Boolean).pop() || '文件库'}</strong><code>{status?.root_path || '尚未配置 File Station 路径'}</code></div><a className="text-button" href="#settings">管理连接</a></div>
    </section>
    <div className="info-note"><Info size={18} /><p>SynoFind 把文件名、类型、大小和修改时间条件交给群晖搜索；复制路径、下载和预览也通过已配置的 DSM 只读账户完成。</p></div>
  </>;
}
