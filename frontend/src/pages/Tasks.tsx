import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2 } from 'lucide-react';

import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import { api } from '../api/client';
import type { Task } from '../api/types';

const STATUSES: Task['status'][] = ['open', 'in_progress', 'blocked', 'done'];

export default function TasksPage(): JSX.Element {
  const qc = useQueryClient();
  const [title, setTitle] = useState('');
  const tasks = useQuery<Task[]>({
    queryKey: ['tasks'],
    queryFn: () => api.get<Task[]>('/api/tasks'),
  });

  const create = useMutation({
    mutationFn: (body: Partial<Task>) => api.post<Task>('/api/tasks', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<Task> }) =>
      api.put<Task>(`/api/tasks/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  });
  const destroy = useMutation({
    mutationFn: (id: string) => api.del<{ ok: boolean }>(`/api/tasks/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  });

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    create.mutate({ title: title.trim() });
    setTitle('');
  };

  return (
    <>
      <PageHeader title="Tasks" description="Every task created by local agents or the dashboard." />
      <form onSubmit={handleCreate} className="card mb-4 flex items-center gap-2">
        <input
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
          placeholder="Add a task"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <button type="submit" className="btn-primary">
          <Plus size={16} /> Add
        </button>
      </form>
      {tasks.data && tasks.data.length === 0 ? (
        <EmptyState title="No tasks yet" description="Create one above or let an agent log them." />
      ) : (
        <div className="card p-0">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-4 py-2">Title</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Priority</th>
                <th className="px-4 py-2">Created</th>
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {(tasks.data ?? []).map((t) => (
                <tr key={t.id} className="border-t border-slate-100">
                  <td className="px-4 py-2">{t.title}</td>
                  <td className="px-4 py-2">
                    <select
                      className="rounded border border-slate-200 px-2 py-1 text-xs"
                      value={t.status}
                      onChange={(e) =>
                        update.mutate({ id: t.id, body: { status: e.target.value as Task['status'] } })
                      }
                    >
                      {STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-4 py-2">{t.priority}</td>
                  <td className="px-4 py-2 text-xs text-slate-500">{t.created_at}</td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() => destroy.mutate(t.id)}
                      className="text-slate-400 hover:text-rose-600"
                      aria-label="delete"
                    >
                      <Trash2 size={14} />
                    </button>
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
