import { useEffect, useState, type FormEvent } from 'react';
import { Check, Code2, Copy, ExternalLink, FolderOpen, KeyRound, LoaderCircle, Plus, Save, ShieldCheck, Trash2, X } from 'lucide-react';
import { api, copyText, isAbort, messageOf } from './api';
import { formatDate } from './format';
import type { Notify, Settings, Token, TokenCreated } from './types';

export function SettingsPage({ notify, onSaved }: { notify: Notify; onSaved: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [savedRoot, setSavedRoot] = useState('');
  const [savedDSMUsername, setSavedDSMUsername] = useState('');
  const [savedHasDSMPassword, setSavedHasDSMPassword] = useState(false);
  const [dsmPassword, setDsmPassword] = useState('');
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
    const controller = new AbortController();
    setLoading(true); setSettingsError(''); setTokensError('');
    const getSettings = api<Settings>('/settings', { signal: controller.signal }).then(value => {
      setSettings(value); setSavedRoot(value.root_path); setSavedDSMUsername(value.dsm_username); setSavedHasDSMPassword(value.has_dsm_password);
    }).catch(reason => { if (!isAbort(reason)) setSettingsError(messageOf(reason)); });
    const getTokens = api<{ items: Token[] }>('/tokens', { signal: controller.signal }).then(value => setTokens(value.items))
      .catch(reason => { if (!isAbort(reason)) setTokensError(messageOf(reason)); });
    void Promise.all([getSettings, getTokens]).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [retry]);

  async function save(event: FormEvent) {
    event.preventDefault(); if (!settings) return;
    setSaving(true); setSettingsError('');
    try {
      const updated = await api<Settings>('/settings', {
        method: 'PUT',
        body: JSON.stringify({
          root_path: settings.root_path, smb_root: settings.smb_root, dsm_url: settings.dsm_url,
          dsm_username: settings.dsm_username, dsm_password: dsmPassword,
        }),
      });
      setSettings(updated); setSavedRoot(updated.root_path); setSavedDSMUsername(updated.dsm_username); setSavedHasDSMPassword(updated.has_dsm_password); setDsmPassword('');
      notify('DSM 连接已验证，设置已保存。'); onSaved();
    } catch (reason) { setSettingsError(messageOf(reason)); }
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
    try {
      await api<void>(`/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' });
      setTokens(items => items.filter(item => item.id !== id)); if (created?.id === id) setCreated(null);
      setRevokeID(''); notify('访问令牌已撤销。');
    } catch (reason) { notify(messageOf(reason), 'error'); }
    finally { setDeleting(''); }
  }

  async function copy(value: string, message: string) {
    try { await copyText(value); notify(message); }
    catch (reason) { notify(messageOf(reason), 'error'); }
  }

  const exampleCode = example === 'curl' ? `# 在环境变量中设置你的访问令牌
curl -G '${window.location.origin}/api/v1/search' \\
  -H "Authorization: Bearer $SYNOFIND_TOKEN" \\
  --data-urlencode 'q=合同' \\
  --data-urlencode 'limit=10'

# 使用搜索结果中的文件 ID 下载
curl '${window.location.origin}/api/v1/files/FILE_ID/content' \\
  -H "Authorization: Bearer $SYNOFIND_TOKEN" \\
  -o 'download.bin'` : `import os
import requests

base = ${JSON.stringify(window.location.origin)}
headers = {"Authorization": "Bearer " + os.environ["SYNOFIND_TOKEN"]}

response = requests.get(
    base + "/api/v1/search",
    params={"q": "合同", "limit": 10},
    headers=headers, timeout=30,
)
response.raise_for_status()
for item in response.json()["items"]:
    print(item["id"], item["name"])

file_id = input("要下载的文件 ID：").strip()
with requests.get(
    base + "/api/v1/files/" + file_id + "/content",
    headers=headers, stream=True, timeout=60,
) as response:
    response.raise_for_status()
    with open("download.bin", "wb") as output:
        for chunk in response.iter_content(1024 * 1024):
            output.write(chunk)`;

  return <>
    <div className="page-heading">
      <div><span className="eyebrow">MAKE IT YOURS</span><h1>设置与 API</h1><p>连接群晖原生搜索，并管理本地读取入口。</p></div>
      <span className="subtle-badge"><ShieldCheck size={14} />只读访问</span>
    </div>

    <section className="panel">
      <div className="panel-heading"><div><h2><FolderOpen size={19} />群晖 File Station 搜索</h2><p>由 DSM 搜索文件名和属性，SynoFind 不建立本地文件索引。</p></div></div>
      {settingsError && <div className="inline-alert error" role="alert">{settingsError}{!settings && <button className="text-button" onClick={() => setRetry(value => value + 1)}>重试</button>}</div>}
      {settings ? <form onSubmit={save} className="settings-form">
        <div className="form-grid">
          <label>DSM API 地址<input required value={settings.dsm_url} onChange={event => setSettings({ ...settings, dsm_url: event.target.value })} placeholder="http://127.0.0.1:5000" disabled={saving} /><span className="field-help">同一台 NAS 默认使用本机地址；自定义端口时请填写对应地址。远程地址须使用 HTTPS。</span></label>
          <label>DSM 只读账户<input required autoComplete="username" value={settings.dsm_username} onChange={event => setSettings({ ...settings, dsm_username: event.target.value, has_dsm_password: event.target.value === savedDSMUsername && savedHasDSMPassword })} disabled={saving} /><span className="field-help">请使用有 File Station 应用权限及目标共享只读权限的 DSM 账户。</span></label>
          <label>DSM 账户密码<input type="password" autoComplete="new-password" value={dsmPassword} onChange={event => setDsmPassword(event.target.value)} placeholder={settings.has_dsm_password ? '已保存；留空则保持不变' : '请输入 DSM 账户密码'} disabled={saving} /><span className="field-help">以明文保存在私有配置文件中（权限 0600），设置读取 API 不会返回密码。</span></label>
          <label>File Station 资料目录<input required value={settings.root_path} onChange={event => setSettings({ ...settings, root_path: event.target.value })} placeholder="/Wonvy" disabled={saving} /><span className="field-help">填写共享文件夹路径，例如 /Wonvy 或 /Wonvy/资料，不是 /volume1/Wonvy。</span></label>
          <label>Windows 共享路径<input value={settings.smb_root} onChange={event => setSettings({ ...settings, smb_root: event.target.value })} placeholder="\\NAS\Wonvy" disabled={saving} /><span className="field-help">用于复制到 Windows 资源管理器的路径。</span></label>
          <label>网页与 API 端口<input value={settings.port} disabled /><span className="field-help">此端口在安装套件时设置。</span></label>
        </div>
        {settings.root_path !== savedRoot && <div className="inline-alert notice">搜索范围已更改；保存前会用 DSM 账户检查目录权限。</div>}
        <div className="form-footer"><span>设置会测试 DSM 登录和目录读取权限</span><button className="button primary" disabled={saving}>{saving ? <LoaderCircle size={16} className="spin" /> : <Save size={16} />}{saving ? '正在验证并保存' : '测试连接并保存'}</button></div>
      </form> : loading && <div className="section-loading"><LoaderCircle size={20} className="spin" />正在加载设置…</div>}
    </section>

    <section className="panel">
      <div className="panel-heading"><div><h2><KeyRound size={19} />API 访问令牌</h2><p>让本地脚本搜索和读取文件。令牌只显示一次，可随时撤销。</p></div></div>
      {tokensError && <div className="inline-alert error" role="alert">{tokensError}<button className="text-button" onClick={() => setRetry(value => value + 1)}>重新加载</button></div>}
      {created && <div className="token-reveal"><div><strong><Check size={16} />令牌已创建：{created.name}</strong><button className="icon-button" title="已保存，关闭显示" aria-label="关闭令牌显示" onClick={() => setCreated(null)}><X size={17} /></button></div><p>请现在复制并妥善保存。关闭后将无法再次查看完整令牌。</p><div className="token-value"><code>{created.token}</code><button className="button secondary small" onClick={() => void copy(created.token, '令牌已复制，请保存在安全的位置。')}><Copy size={14} />复制</button></div></div>}
      <form className="token-create" onSubmit={create}><label className="sr-only" htmlFor="token-name">令牌名称</label><input id="token-name" value={name} onChange={event => setName(event.target.value)} placeholder="为令牌命名，例如：电脑上的 Python 脚本" maxLength={80} required disabled={creating} /><button className="button secondary" disabled={creating || loading || !name.trim() || !!created}>{creating ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}创建令牌</button></form>
      {created && <p className="field-help token-hint">保存并关闭上方令牌后，可以继续创建。</p>}
      {loading ? <div className="section-loading"><LoaderCircle className="spin" size={18} />正在加载令牌…</div> : tokens.length === 0 && !tokensError ? <div className="token-empty"><KeyRound size={22} /><span>还没有访问令牌。创建后即可连接你的本地程序。</span></div> : <div className="token-list">{tokens.map(token => <div className="token-row" key={token.id}><span className="token-icon"><KeyRound size={18} /></span><div><strong>{token.name}</strong><span>创建于 {formatDate(token.created_at)}<span className="meta-divider">·</span>{token.last_used_at ? `最近使用 ${formatDate(token.last_used_at)}` : '尚未使用'}</span></div>{revokeID === token.id ? <div className="revoke-confirm"><span>撤销此令牌？</span><button className="button danger small" disabled={deleting === token.id} onClick={() => void revoke(token.id)}>{deleting === token.id ? '撤销中…' : '确认撤销'}</button><button className="button ghost small" disabled={!!deleting} onClick={() => setRevokeID('')}>取消</button></div> : <button className="icon-button danger-icon" title="撤销令牌" aria-label={`撤销 ${token.name}`} onClick={() => setRevokeID(token.id)}><Trash2 size={16} /></button>}</div>)}</div>}
    </section>

    <section className="panel api-panel">
      <div className="panel-heading"><div><h2><Code2 size={19} />从你的程序获取文件</h2><p>搜索由 DSM File Station.Search 执行，不会向本地建立索引。</p></div><a href="/api/openapi.json" target="_blank" rel="noreferrer" className="text-button">OpenAPI 文档 <ExternalLink size={14} /></a></div>
      <div className="code-header"><div className="code-tabs" role="tablist" aria-label="调用示例语言"><button role="tab" aria-selected={example === 'curl'} onClick={() => setExample('curl')}>cURL</button><button role="tab" aria-selected={example === 'python'} onClick={() => setExample('python')}>Python</button></div><button className="code-copy" onClick={() => void copy(exampleCode, '调用示例已复制。')}><Copy size={14} />复制示例</button></div>
      <pre className="code-block"><code>{exampleCode}</code></pre><p className="code-note">令牌通过 Authorization 请求头传递；请勿将令牌放入网址或公开代码。Python 示例需要安装 requests。</p>
    </section>
  </>;
}
