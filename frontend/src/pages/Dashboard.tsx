import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  Activity,
  AlertTriangle,
  BookOpenText,
  ClipboardList,
  Layers,
  ListChecks,
  Network,
  type LucideIcon,
} from 'lucide-react';

import PageHeader from '../components/PageHeader';
import { api } from '../api/client';
import type { Blocker, Session, Stats, Task } from '../api/types';

interface StatCard {
  label: string;
  value: string | number;
  to: string;
  icon: LucideIcon;
}

export default function Dashboard(): JSX.Element {
  const stats = useQuery<Stats>({
    queryKey: ['stats'],
    queryFn: () => api.get<Stats>('/api/stats'),
  });
  const recentTasks = useQuery<Task[]>({
    queryKey: ['tasks'],
    queryFn: () => api.get<Task[]>('/api/tasks'),
  });
  const activeSessions = useQuery<Session[]>({
    queryKey: ['sessions', { active: true }],
    queryFn: () => api.get<Session[]>('/api/sessions?active=true'),
  });
  const activeBlockers = useQuery<Blocker[]>({
    queryKey: ['blockers', { status: 'active' }],
    queryFn: () => api.get<Blocker[]>('/api/blockers?status=active'),
  });

  const s = stats.data;
  const cards: StatCard[] = [
    { label: 'Open tasks', value: s?.tasks.open ?? '—', to: '/tasks', icon: ListChecks },
    { label: 'Active sessions', value: s?.sessions.active ?? '—', to: '/sessions', icon: Activity },
    { label: 'Active blockers', value: s?.blockers.active ?? '—', to: '/blockers', icon: AlertTriangle },
    { label: 'Decisions', value: s?.decisions ?? '—', to: '/decisions', icon: BookOpenText },
    { label: 'Work logs', value: s?.work_logs ?? '—', to: '/work-logs', icon: ClipboardList },
    { label: 'Code Atlas nodes', value: s?.nodes ?? '—', to: '/graph', icon: Network },
    { label: 'Scanned files', value: s?.edges ?? '—', to: '/code-atlas', icon: Layers },
  ];

  return (
    <>
      <PageHeader title="Dashboard" description="Live view of the OBSMCP observability surface." />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {cards.map((c) => (
          <Link to={c.to} key={c.label} className="card hover:shadow-md">
            <div className="flex items-center gap-3">
              <c.icon size={22} className="text-brand-600" />
              <div>
                <div className="text-2xl font-semibold">{c.value}</div>
                <div className="text-xs text-slate-600">{c.label}</div>
              </div>
            </div>
          </Link>
        ))}
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <section className="card">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Recent tasks</h2>
          {(recentTasks.data ?? []).slice(0, 5).map((t) => (
            <div key={t.id} className="border-t border-slate-100 py-2 text-sm first:border-t-0">
              <div className="font-medium">{t.title}</div>
              <div className="text-xs text-slate-500">
                {t.status} · {t.priority}
              </div>
            </div>
          ))}
          {recentTasks.data && recentTasks.data.length === 0 && (
            <p className="text-sm text-slate-500">No tasks yet.</p>
          )}
        </section>
        <section className="card">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Active sessions</h2>
          {(activeSessions.data ?? []).slice(0, 5).map((s2) => (
            <div key={s2.id} className="border-t border-slate-100 py-2 text-sm first:border-t-0">
              <div className="font-medium">{s2.agent_id}</div>
              <div className="text-xs text-slate-500">since {s2.started_at}</div>
            </div>
          ))}
          {activeSessions.data && activeSessions.data.length === 0 && (
            <p className="text-sm text-slate-500">No active sessions.</p>
          )}
        </section>
        <section className="card">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Active blockers</h2>
          {(activeBlockers.data ?? []).slice(0, 5).map((b) => (
            <div key={b.id} className="border-t border-slate-100 py-2 text-sm first:border-t-0">
              <div className="font-medium">{b.description}</div>
              <div className="text-xs text-slate-500">{b.severity}</div>
            </div>
          ))}
          {activeBlockers.data && activeBlockers.data.length === 0 && (
            <p className="text-sm text-slate-500">Nothing blocking.</p>
          )}
        </section>
      </div>
    </>
  );
}
