import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import { api } from '../api/client';
import type { Decision } from '../api/types';

export default function DecisionsPage(): JSX.Element {
  const qc = useQueryClient();
  const [decision, setDecision] = useState('');
  const [context, setContext] = useState('');
  const decisions = useQuery<Decision[]>({
    queryKey: ['decisions'],
    queryFn: () => api.get<Decision[]>('/api/decisions'),
  });
  const create = useMutation({
    mutationFn: (body: Partial<Decision>) => api.post<Decision>('/api/decisions', body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['decisions'] });
      setDecision('');
      setContext('');
    },
  });

  return (
    <>
      <PageHeader title="Decisions" description="Architectural & product decisions with rationale." />
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!decision.trim()) return;
          create.mutate({ decision: decision.trim(), context: context.trim() || null });
        }}
        className="card mb-4 space-y-2"
      >
        <input
          value={decision}
          onChange={(e) => setDecision(e.target.value)}
          placeholder="Decision title"
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <textarea
          value={context}
          onChange={(e) => setContext(e.target.value)}
          placeholder="Context / reasoning (optional)"
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          rows={3}
        />
        <div className="text-right">
          <button type="submit" className="btn-primary">
            Log decision
          </button>
        </div>
      </form>
      {decisions.data && decisions.data.length === 0 ? (
        <EmptyState title="No decisions logged" />
      ) : (
        <div className="space-y-3">
          {(decisions.data ?? []).map((d) => (
            <article key={d.id} className="card">
              <h3 className="font-medium">{d.decision}</h3>
              {d.context && <p className="mt-1 text-sm text-slate-600">{d.context}</p>}
              <p className="mt-2 text-xs text-slate-500">{d.created_at}</p>
            </article>
          ))}
        </div>
      )}
    </>
  );
}
