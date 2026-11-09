import { useEffect, useState, type FormEvent } from 'react';
import { Check, Code2, Copy, ExternalLink, FolderOpen, KeyRound, LoaderCircle, Plus, Save, ShieldCheck, Trash2, X } from 'lucide-react';
import { api, copyText, isAbort, messageOf } from './api';
import { formatDate } from './format';
import type { Notify, Settings, Token, TokenCreated } from './types';

export function SettingsPage({ notify, onSaved }: { notify: Notify; onSaved: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [savedRoot, setSavedRoot] = useState('');
  const [tokens, setTokens] = useState<Token[]>([]);
  const [settingsError, setSettingsError] = useState('');
  const [tokensError, setTokensError] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<TokenCreated | null>(null);
  const [revokeID, setRevokeID] = useState('');
  const [deleting, setDeleting] = useState('');
  const [retry, setRetry] = useState(0);
  const [example, setExample] = useState<'curl' | 'python'>('curl');
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setSettingsError(''); setTokensError('');
    const getSettings = api<Settings>('/settings', { signal: controller.signal }).then(value => { setSettings(value); setSavedRoot(value.root_path); }).catch(reason => { if (!isAbort(reason)) setSettingsError(messageOf(reason)); });
    const getTokens = api<{ items: Token[] }>('/tokens', { signal: controller.signal }).then(value => setTokens(value.items)).catch(reason => { if (!isAbort(reason)) setTokensError(messageOf(reason)); });
    void Promise.all([getSettings, getTokens]).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [retry]);
  async function save(event: FormEvent) {
    event.preventDefault(); if (!settings) return;
    setSaving(true); setSettingsError('');
    try { await api<Settings>('/settings', { method: 'PUT', body: JSON.stringify(settings) }); setSavedRoot(settings.root_path); notify('设置已保存。'); onSaved(); }
    catch (reason) { setSettingsError(messageOf(reason)); }
    finally { setSaving(false); }
  }
  async function create(event: FormEvent) {
    event.preventDefault(); setCreating(true); setTokensError('');
    try {
      const value = await api<TokenCreated>('/tokens', { method: 'POST', body: JSON.stringify({ name: name.trim() }) });
      setCreated(value); setName(''); setTokens(items => [...items, { id: value.id, name: value.name, created_at: value.created_at, last_used_at: '' }]);
      notify('访问令牌已创建，请复制保存。');
    } catch (reason) { setTokensError(messageOf(reason)); }
    finally { setCreating(false); }
  }
  async function revoke(id: string) {
    setDeleting(id);
    try { await api<void>(`/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' }); setTokens(items => items.filter(item => item.id !== id)); if (created?.id === id) setCreated(null); setRevokeID(''); notify('访问令牌已撤销。'); }
    catch (reason) { notify(messageOf(reason), 'error'); }
    finally { setDeleting(''); }
  }
  async function copy(value: string, message: string) { try { await copyText(value); notify(message); } catch (reason) { notify(messageOf(reason), 'error'); } }
  const exampleCode = example === 'curl' ? `# 在环境变量中设置你的访问令牌\ncurl -G '${window.location.origin}/api/v1/search' \\\n  -H "Authorization: Bearer $SYNOFIND_TOKEN" \\\n  --data-urlencode 'q=合同' \\\n  --data-urlencode 'limit=10'\n\n# 使用搜索结果中的文件 ID 下载\ncurl '${window.location.origin}/api/v1/files/FILE_ID/content' \\\n  -H "Authorization: Bearer $SYNOFIND_TOKEN" \\\n  -o 'download.bin'` : `import os\nimport requests\n\nbase = ${JSON.stringify(window.location.origin)}\nheaders = {"Authorization": "Bearer " + os.environ["SYNOFIND_TOKEN"]}\n\nresponse = requests.get(\n    base + "/api/v1/search",\n    params={"q": "合同", "limit": 10},\n    headers=headers, timeout=30,\n)\nresponse.raise_for_status()\nfor item in response.json()["items"]:\n    print(item["id"], item["name"])\n\nfile_id = input("要下载的文件 ID：").strip()\nwith requests.get(\n    base + "/api/v1/files/" + file_id + "/content",\n    headers=headers, stream=True, timeout=60,\n) as response:\n    response.raise_for_status()\n    with open("download.bin", "wb") as output:\n        for chunk in response.iter_content(1024 * 1024):\n            output.write(chunk)`;
  return <><div className="page-heading"><div><span className="eyebrow">MAKE IT YOURS</span><h1>设置与 API</h1><p>连接你的文件目录，也连接你日常使用的程序。</p></div><span className="subtle-badge"><ShieldCheck size={14} />个人账户</span></div><section className="panel"><div className="panel-heading"><div><h2><FolderOpen size={19} />文件来源与更新</h2><p>搜索范围由目录配置与套件账户的读取权限共同决定。</p></div></div>{settingsError && <div className="inline-alert error" role="alert">{settingsError}{!settings && <button className="text-button" onClick={() => setRetry(value => value + 1)}>重试</button>}</div>}{settings ? <form onSubmit={save} className="settings-form"><div className="form-grid"><label>群晖文件目录<input required value={settings.root_path} onChange={event => setSettings({ ...settings, root_path: event.target.value })} placeholder="/volume1/Wonvy" disabled={saving} /><span className="field-help">需在 DSM 为 SynoFind 系统内部用户授予只读权限。</span></label><label>Windows 共享路径<input required value={settings.smb_root} onChange={event => setSettings({ ...settings, smb_root: event.target.value })} placeholder="\\NAS\Wonvy" disabled={saving} /><span className="field-help">用于“复制路径”，与上面的目录指向同一位置。</span></label><label>自动更新间隔（分钟）<input type="number" required min="1" max="1440" value={settings.scan_interval_minutes} onChange={event => setSettings({ ...settings, scan_interval_minutes: Number(event.target.value) })} disabled={saving} /><span className="field-help">扫描任务不会重叠；也可随时手动刷新。</span></label><label>网页与 API 端口<input value={settings.port} disabled /><span className="field-help">此端口在安装套件时设置。</span></label></div>{settings.root_path !== savedRoot && <div className="inline-alert notice">更换目录会重建索引，原始文件不会被修改。</div>}<div className="form-footer"><span>文件仅以只读方式访问</span><button className="button primary" disabled={saving}>{saving ? <LoaderCircle size={16} className="spin" /> : <Save size={16} />}{saving ? '正在验证并保存' : '保存设置'}</button></div></form> : loading && <div className="section-loading"><LoaderCircle size={20} className="spin" />正在加载设置…</div>}</section><section className="panel"><div className="panel-heading"><div><h2><KeyRound size={19} />API 访问令牌</h2><p>让本地脚本搜索和读取文件。令牌只显示一次，可随时撤销。</p></div></div>{tokensError && <div className="inline-alert error" role="alert">{tokensError}<button className="text-button" onClick={() => setRetry(value => value + 1)}>重新加载</button></div>}{created && <div className="token-reveal"><div><strong><Check size={16} />令牌已创建：{created.name}</strong><button className="icon-button" title="已保存，关闭显示" aria-label="关闭令牌显示" onClick={() => setCreated(null)}><X size={17} /></button></div><p>请现在复制并妥善保存。关闭后将无法再次查看完整令牌。</p><div className="token-value"><code>{created.token}</code><button className="button secondary small" onClick={() => void copy(created.token, '令牌已复制，请保存在安全的位置。')}><Copy size={14} />复制</button></div></div>}<form className="token-create" onSubmit={create}><label className="sr-only" htmlFor="token-name">令牌名称</label><input id="token-name" value={name} onChange={event => setName(event.target.value)} placeholder="为令牌命名，例如：电脑上的 Python 脚本" maxLength={80} required disabled={creating} /><button className="button secondary" disabled={creating || loading || !name.trim() || !!created}>{creating ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}创建令牌</button></form>{created && <p className="field-help token-hint">保存并关闭上方令牌后，可以继续创建。</p>}{loading ? <div className="section-loading"><LoaderCircle className="spin" size={18} />正在加载令牌…</div> : tokens.length === 0 && !tokensError ? <div className="token-empty"><KeyRound size={22} /><span>还没有访问令牌。创建后即可连接你的本地程序。</span></div> : <div className="token-list">{tokens.map(token => <div className="token-row" key={token.id}><span className="token-icon"><KeyRound size={18} /></span><div><strong>{token.name}</strong><span>创建于 {formatDate(token.created_at)}<span className="meta-divider">·</span>{token.last_used_at ? `最近使用 ${formatDate(token.last_used_at)}` : '尚未使用'}</span></div>{revokeID === token.id ? <div className="revoke-confirm"><span>撤销此令牌？</span><button className="button danger small" disabled={deleting === token.id} onClick={() => void revoke(token.id)}>{deleting === token.id ? '撤销中…' : '确认撤销'}</button><button className="button ghost small" disabled={!!deleting} onClick={() => setRevokeID('')}>取消</button></div> : <button className="icon-button danger-icon" title="撤销令牌" aria-label={`撤销 ${token.name}`} onClick={() => setRevokeID(token.id)}><Trash2 size={16} /></button>}</div>)}</div>}</section><section className="panel api-panel"><div className="panel-heading"><div><h2><Code2 size={19} />从你的程序获取文件</h2><p>网页与 API 共用同一份文件索引。</p></div><a href="/api/openapi.json" target="_blank" rel="noreferrer" className="text-button">OpenAPI 文档 <ExternalLink size={14} /></a></div><div className="code-header"><div className="code-tabs" role="tablist" aria-label="调用示例语言"><button role="tab" aria-selected={example === 'curl'} onClick={() => setExample('curl')}>cURL</button><button role="tab" aria-selected={example === 'python'} onClick={() => setExample('python')}>Python</button></div><button className="code-copy" onClick={() => void copy(exampleCode, '调用示例已复制。')}><Copy size={14} />复制示例</button></div><pre className="code-block"><code>{exampleCode}</code></pre><p className="code-note">令牌通过 Authorization 请求头传递；请勿将令牌放入网址或公开代码。Python 示例需要安装 requests。</p></section></>;
}

