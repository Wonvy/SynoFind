import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowRight, Check, CircleAlert, Database, FolderSearch, LoaderCircle, LockKeyhole, LogOut, Search, Settings2, ShieldCheck, X } from 'lucide-react';
import { api, ApiError, isAbort, messageOf, setCsrfToken } from './api';
import { SearchPage } from './SearchPage';
import { IndexPage } from './IndexPage';
import { SettingsPage } from './SettingsPage';
import type { IndexStatus, Notify, Page, Session } from './types';

function initialPage(): Page {
  const value = window.location.hash.slice(1);
  return value === 'index' || value === 'settings' ? value : 'search';
}

export function App() {
  const [auth, setAuth] = useState<'loading' | 'login' | 'ready'>('loading');
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setAuth('loading'); setError('');
    api<Session>('/auth/session', { signal: controller.signal })
      .then(value => { setCsrfToken(value.csrf_token); setAuth('ready'); })
      .catch(reason => {
        if (isAbort(reason)) return;
        if (reason instanceof ApiError && reason.status === 401) setAuth('login');
        else setError(messageOf(reason));
      });
    return () => controller.abort();
  }, [attempt]);
  useEffect(() => {
    const onUnauthorized = () => { setAuth('login'); setError('登录已过期，请重新输入密码。'); };
    window.addEventListener('synofind:unauthorized', onUnauthorized);
    return () => window.removeEventListener('synofind:unauthorized', onUnauthorized);
  }, []);
  if (auth === 'loading') return <div className="boot-screen"><Brand large />{error ? <><p className="error-text">{error}</p><button className="button primary" onClick={() => setAttempt(value => value + 1)}>重新连接</button></> : <p><LoaderCircle className="spin" size={17} /> 正在连接文件库</p>}</div>;
  if (auth === 'login') return <Login notice={error} onLogin={session => { setCsrfToken(session.csrf_token); setError(''); setAuth('ready'); }} />;
  return <Shell onLogout={() => { setCsrfToken(''); setAuth('login'); }} />;
}

export function Brand({ large = false }: { large?: boolean }) {
  return <div className={`brand ${large ? 'brand-large' : ''}`}><span className="brand-mark"><FolderSearch size={large ? 30 : 25} strokeWidth={1.8} /></span><span>Syno<span className="brand-accent">Find</span></span></div>;
}

function Login({ notice, onLogin }: { notice: string; onLogin: (session: Session) => void }) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(notice);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try { const result = await api<Session>('/auth/login', { method: 'POST', body: JSON.stringify({ password }) }); setPassword(''); onLogin(result); }
    catch (reason) { setError(messageOf(reason)); }
    finally { setBusy(false); }
  }
  return <div className="login-layout"><div className="login-story"><Brand large /><div className="login-copy"><span className="eyebrow">YOUR FILES, WITHIN REACH</span><h1>文件在那里，<br />一搜就找到。</h1><p>为你的群晖准备的轻量搜索空间。<br />查找资料、浏览文件，让每次寻找都更简单。</p><div className="login-feature"><ShieldCheck size={18} />文件与索引保留在你的群晖</div></div><div className="login-caption">SynoFind · 私人文件搜索</div></div><main className="login-main"><form className="login-card" onSubmit={submit}><div className="login-icon"><LockKeyhole size={25} /></div><h2>欢迎回来</h2><p>输入安装时设置的 SynoFind 密码。</p><label className="field-label" htmlFor="password">登录密码</label><input id="password" type="password" autoComplete="current-password" autoFocus required value={password} onChange={event => setPassword(event.target.value)} placeholder="请输入密码" disabled={busy} />{error && <div className="inline-alert error" role="alert"><CircleAlert size={16} />{error}</div>}<button className="button primary login-submit" disabled={busy || !password}>{busy ? <LoaderCircle size={18} className="spin" /> : <ArrowRight size={18} />} {busy ? '正在登录' : '进入文件库'}</button><p className="login-footnote">这是 SynoFind 的独立密码，与 DSM 密码不同。</p></form></main></div>;
}

function Shell({ onLogout }: { onLogout: () => void }) {
  const [page, setPage] = useState<Page>(initialPage);
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [statusError, setStatusError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [toast, setToast] = useState<{ message: string; kind: 'success' | 'error' } | null>(null);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const notify: Notify = useCallback((message, kind = 'success') => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast({ message, kind });
    toastTimer.current = setTimeout(() => setToast(null), 5500);
  }, []);
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);
  useEffect(() => {
    const update = () => setPage(initialPage());
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const poll = async () => {
      try { const result = await api<IndexStatus>('/index/status', { signal: controller.signal }); setStatus(result); setStatusError(''); }
      catch (reason) { if (!isAbort(reason)) setStatusError(messageOf(reason)); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);
  async function logout() {
    setLogoutBusy(true);
    try { await api<void>('/auth/logout', { method: 'POST' }); onLogout(); }
    catch (reason) { notify(messageOf(reason), 'error'); }
    finally { setLogoutBusy(false); }
  }
  const items = [{ id: 'search' as Page, label: '文件搜索', icon: Search }, { id: 'index' as Page, label: '索引状态', icon: Database }, { id: 'settings' as Page, label: '设置与 API', icon: Settings2 }];
  return <div className="app-layout"><aside className="sidebar"><a className="brand-link" href="#search" aria-label="SynoFind 首页"><Brand /></a><div className="nav-label">文件工作区</div><nav aria-label="主导航">{items.map(({ id, label, icon: Icon }) => <a key={id} href={`#${id}`} aria-current={page === id ? 'page' : undefined} className={`nav-item ${page === id ? 'active' : ''}`}><Icon size={19} strokeWidth={1.8} /><span>{label}</span>{id === 'index' && status?.running && <span className="nav-dot" />}</a>)}</nav><div className="sidebar-bottom"><div className="server-card"><span className={`status-dot ${statusError ? 'offline' : status ? '' : 'pending'}`} /><div><strong>{statusError ? '连接中断' : status ? '群晖已连接' : '正在连接'}</strong><span>{window.location.hostname}</span></div></div><button className="logout-button" onClick={logout} disabled={logoutBusy}><LogOut size={16} />退出登录</button><span className="sidebar-version">SynoFind <span>v0.1.0</span></span></div></aside><div className="main-shell"><header className="topbar"><div className="breadcrumb">我的文件库<span>/</span><strong>{items.find(item => item.id === page)?.label}</strong></div><div className="private-label"><LockKeyhole size={13} /><span>个人空间</span><button className="icon-button mobile-logout" title="退出登录" aria-label="退出登录" disabled={logoutBusy} onClick={logout}><LogOut size={15} /></button></div></header><main className="main-content">{page === 'search' && <SearchPage status={status} notify={notify} />}{page === 'index' && <IndexPage status={status} error={statusError} refresh={() => setRefresh(value => value + 1)} notify={notify} />}{page === 'settings' && <SettingsPage notify={notify} onSaved={() => setRefresh(value => value + 1)} />}</main><footer className="app-footer"><span>文件留在群晖，寻找更进一步。</span><a href="/api/openapi.json" target="_blank" rel="noreferrer">API 文档 <ArrowRight size={12} /></a></footer></div>{toast && <div className={`toast ${toast.kind}`} role={toast.kind === 'error' ? 'alert' : 'status'}>{toast.kind === 'success' ? <Check size={18} /> : <CircleAlert size={18} />}<span>{toast.message}</span><button aria-label="关闭提示" onClick={() => setToast(null)}><X size={16} /></button></div>}</div>;
}

