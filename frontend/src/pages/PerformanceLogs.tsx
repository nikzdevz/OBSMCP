import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import { api } from '../api/client';
import type { PerformanceLog } from '../api/types';

export default function PerformanceLogsPage(): JSX.Element {
  const logs = useQuery<PerformanceLog[]>({
    queryKey: ['performance-logs'],
    queryFn: () => api.get<PerformanceLog[]>('/api/performance-logs?limit=500'),
  });

  const series = useMemo(() => {
    const byTs: Record<string, Record<string, number | string>> = {};
    for (const l of logs.data ?? []) {
      const ts = l.logged_at.slice(0, 19);
      byTs[ts] = byTs[ts] ?? { time: ts };
      byTs[ts][l.metric_name] = l.metric_value;
    }
    return Object.values(byTs).sort((a, b) => (a.time as string).localeCompare(b.time as string));
  }, [logs.data]);

  const metricNames = useMemo(() => {
    const names = new Set<string>();
    for (const l of logs.data ?? []) names.add(l.metric_name);
    return [...names];
  }, [logs.data]);

  return (
    <>
      <PageHeader title="Performance Logs" description="CPU, memory, disk sampled every 30s." />
      {series.length === 0 ? (
        <EmptyState title="No performance data" description="Logs will appear once the agent runs." />
      ) : (
        <div className="card">
          <ResponsiveContainer width="100%" height={360}>
            <LineChart data={series}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" tick={{ fontSize: 10 }} />
              <YAxis />
              <Tooltip />
              {metricNames.map((m, i) => (
                <Line
                  key={m}
                  type="monotone"
                  dataKey={m}
                  stroke={['#5b71ff', '#f97316', '#22c55e', '#ef4444'][i % 4]}
                  dot={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </>
  );
}
