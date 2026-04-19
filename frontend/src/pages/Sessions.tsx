import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import { api } from '../api/client';
import type { Session } from '../api/types';

export default function SessionsPage(): JSX.Element {
  const qc = useQueryClient();
  const sessions = useQuery<Session[]>({
    queryKey: ['sessions'],
    queryFn: () => api.get<Session[]>('/api/sessions'),
  });
  const close = useMutation({
    mutationFn: (id: string) => api.put<Session>(`/api/sessions/${id}/close`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sessions'] }),
  });

  return (
    <>
      <PageHeader title="Sessions" description="Agent sessions with live heartbeat state." />
      {sessions.data && sessions.data.length === 0 ? (
        <EmptyState title="No sessions" description="Sessions appear once the local tool launches." />
      ) : (
        <div className="card p-0">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-4 py-2">Agent</th>
                <th className="px-4 py-2">Started</th>
                <th className="px-4 py-2">Ended</th>
                <th className="px-4 py-2">Duration</th>
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {(sessions.data ?? []).map((s) => (
                <tr key={s.id} className="border-t border-slate-100">
                  <td className="px-4 py-2 font-medium">{s.agent_id}</td>
                  <td className="px-4 py-2 text-xs text-slate-500">{s.started_at}</td>
                  <td className="px-4 py-2 text-xs text-slate-500">{s.ended_at ?? '—'}</td>
                  <td className="px-4 py-2 text-xs">
                    {s.duration_seconds != null ? `${s.duration_seconds}s` : 'active'}
                  </td>
                  <td className="px-4 py-2">
                    {!s.ended_at && (
                      <button onClick={() => close.mutate(s.id)} className="btn-secondary text-xs">
                        Close
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
