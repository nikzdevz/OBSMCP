import { useQuery } from '@tanstack/react-query';
import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import { api } from '../api/client';
import type { WorkLog } from '../api/types';

export default function WorkLogsPage(): JSX.Element {
  const logs = useQuery<WorkLog[]>({
    queryKey: ['work-logs'],
    queryFn: () => api.get<WorkLog[]>('/api/work-logs'),
  });
  return (
    <>
      <PageHeader title="Work Logs" description="Everything the agent has done in this project." />
      {logs.data && logs.data.length === 0 ? (
        <EmptyState title="No work logged yet" />
      ) : (
        <div className="card p-0">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-4 py-2">When</th>
                <th className="px-4 py-2">Agent</th>
                <th className="px-4 py-2">Description</th>
                <th className="px-4 py-2">Hours</th>
              </tr>
            </thead>
            <tbody>
              {(logs.data ?? []).map((l) => (
                <tr key={l.id} className="border-t border-slate-100">
                  <td className="px-4 py-2 text-xs text-slate-500">{l.created_at}</td>
                  <td className="px-4 py-2 text-xs">{l.agent_id ?? '—'}</td>
                  <td className="px-4 py-2">{l.description}</td>
                  <td className="px-4 py-2">{l.hours ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
