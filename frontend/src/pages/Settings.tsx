import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import PageHeader from '../components/PageHeader';
import { api, clearApiToken, getApiToken, setApiToken } from '../api/client';

interface ModeResponse {
  mode: 'local' | 'cloud';
}
interface Runtime {
  version: string;
  mode: string;
  features: string[];
  db_schema_version: number;
}

export default function SettingsPage(): JSX.Element {
  const [token, setToken] = useState(getApiToken() ?? '');
  const mode = useQuery<ModeResponse>({ queryKey: ['mode'], queryFn: () => api.get<ModeResponse>('/mode') });
  const runtime = useQuery<Runtime>({
    queryKey: ['runtime-discovery'],
    queryFn: () => api.get<Runtime>('/runtime-discovery'),
  });

  const save = () => {
    if (token.trim()) setApiToken(token.trim());
    else clearApiToken();
    window.location.reload();
  };

  return (
    <>
      <PageHeader title="Settings" description="Configure your dashboard connection." />
      <section className="card mb-4">
        <h2 className="text-sm font-semibold">Server</h2>
        <dl className="mt-3 grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-slate-500">Version</dt>
          <dd>{runtime.data?.version ?? '—'}</dd>
          <dt className="text-slate-500">Mode</dt>
          <dd>{mode.data?.mode ?? runtime.data?.mode ?? '—'}</dd>
          <dt className="text-slate-500">Features</dt>
          <dd className="flex flex-wrap gap-1">
            {(runtime.data?.features ?? []).map((f) => (
              <span key={f} className="tag bg-slate-100 text-slate-700">
                {f}
              </span>
            ))}
          </dd>
        </dl>
      </section>
      <section className="card">
        <h2 className="text-sm font-semibold">API token</h2>
        <p className="mt-1 text-xs text-slate-500">
          Bearer token sent with every request. Leave blank for standalone / local mode.
        </p>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          className="mt-3 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          placeholder="Paste your token"
        />
        <div className="mt-3 text-right">
          <button className="btn-primary" onClick={save}>
            Save & reload
          </button>
        </div>
      </section>
    </>
  );
}
