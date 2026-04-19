import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import { api, buildQuery } from '../api/client';
import type { Blocker } from '../api/types';
import { useCurrentProjectId } from '../stores/project';

export default function BlockersPage(): JSX.Element {
  const qc = useQueryClient();
  const projectId = useCurrentProjectId();
  const [description, setDescription] = useState('');
  const [severity, setSeverity] = useState<Blocker['severity']>('medium');
  const [resolution, setResolution] = useState<Record<string, string>>({});

  const blockers = useQuery<Blocker[]>({
    queryKey: ['blockers', { projectId }],
    queryFn: () => api.get<Blocker[]>(buildQuery('/api/blockers', { project_id: projectId })),
  });
  const create = useMutation({
    mutationFn: (body: Partial<Blocker>) =>
      api.post<Blocker>('/api/blockers', { ...body, project_id: projectId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['blockers'] }),
  });
  const resolve = useMutation({
    mutationFn: ({ id, r }: { id: string; r: string }) =>
      api.put<Blocker>(`/api/blockers/${id}/resolve`, { resolution: r }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['blockers'] }),
  });

  return (
    <>
      <PageHeader title="Blockers" description="Log and resolve anything blocking progress." />
      <form
        className="card mb-4 grid grid-cols-[1fr_auto_auto] gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!description.trim()) return;
          create.mutate({ description: description.trim(), severity });
          setDescription('');
        }}
      >
        <input
          className="rounded-md border border-slate-300 px-3 py-2 text-sm"
          placeholder="Describe the blocker"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <select
          className="rounded-md border border-slate-300 px-2 text-sm"
          value={severity}
          onChange={(e) => setSeverity(e.target.value as Blocker['severity'])}
        >
          {(['low', 'medium', 'high', 'critical'] as const).map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <button type="submit" className="btn-primary">
          Log
        </button>
      </form>
      {blockers.data && blockers.data.length === 0 ? (
        <EmptyState title="No blockers" description="Create one above to track an issue." />
      ) : (
        <div className="space-y-3">
          {(blockers.data ?? []).map((b) => (
            <article key={b.id} className="card">
              <header className="flex items-center justify-between">
                <h3 className="font-medium">{b.description}</h3>
                <span
                  className={`tag ${
                    b.status === 'resolved' ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'
                  }`}
                >
                  {b.status}
                </span>
              </header>
              <p className="mt-1 text-xs text-slate-500">
                severity: {b.severity} · logged {b.created_at}
              </p>
              {b.status === 'active' ? (
                <div className="mt-3 flex gap-2">
                  <input
                    className="flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm"
                    placeholder="Resolution…"
                    value={resolution[b.id] ?? ''}
                    onChange={(e) => setResolution((s) => ({ ...s, [b.id]: e.target.value }))}
                  />
                  <button
                    className="btn-secondary"
                    onClick={() => {
                      const r = resolution[b.id]?.trim();
                      if (!r) return;
                      resolve.mutate({ id: b.id, r });
                    }}
                  >
                    Resolve
                  </button>
                </div>
              ) : (
                <p className="mt-2 text-sm text-slate-600">Resolved: {b.resolution}</p>
              )}
            </article>
          ))}
        </div>
      )}
    </>
  );
}
