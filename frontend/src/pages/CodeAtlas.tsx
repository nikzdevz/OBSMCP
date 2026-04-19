import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Play } from 'lucide-react';

import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import { api, buildQuery } from '../api/client';
import type { CodeAtlasFile, CodeAtlasScan } from '../api/types';
import { useCurrentProjectId } from '../stores/project';

const COLORS = ['#5b71ff', '#f97316', '#22c55e', '#ef4444', '#a855f7', '#eab308', '#06b6d4'];

export default function CodeAtlasPage(): JSX.Element {
  const qc = useQueryClient();
  const projectId = useCurrentProjectId();
  const scans = useQuery<CodeAtlasScan[]>({
    queryKey: ['code-atlas', 'scans', { projectId }],
    queryFn: () =>
      api.get<CodeAtlasScan[]>(buildQuery('/api/code-atlas', { project_id: projectId })),
  });
  const latestScan = scans.data?.[0];
  const filesQuery = useQuery<{ files: CodeAtlasFile[] }>({
    queryKey: ['code-atlas', 'files', latestScan?.id],
    enabled: !!latestScan?.id,
    queryFn: () =>
      api.get<{ files: CodeAtlasFile[] }>(`/api/code-atlas/scan/${latestScan!.id}/files?per_page=500`),
  });
  const start = useMutation({
    mutationFn: () =>
      api.post<CodeAtlasScan>('/api/code-atlas/scan', { project_id: projectId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['code-atlas'] }),
  });

  const files = filesQuery.data?.files ?? [];
  const [selected, setSelected] = useState<CodeAtlasFile | null>(null);

  const languageDistribution = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const f of files) {
      const key = f.language ?? 'unknown';
      counts[key] = (counts[key] ?? 0) + 1;
    }
    return Object.entries(counts).map(([name, value]) => ({ name, value }));
  }, [files]);

  const topFunctions = useMemo(
    () =>
      [...files]
        .sort((a, b) => b.functions_count - a.functions_count)
        .slice(0, 10)
        .map((f) => ({ name: f.file_path.split('/').pop() ?? f.file_path, count: f.functions_count })),
    [files],
  );

  return (
    <>
      <PageHeader
        title="Code Atlas"
        description="Language-aware codebase scan with semantic descriptions."
        actions={
          <button
            className="btn-primary"
            disabled={start.isPending}
            onClick={() => start.mutate()}
          >
            <Play size={14} /> {start.isPending ? 'Starting…' : 'Run scan'}
          </button>
        }
      />
      {!latestScan ? (
        <EmptyState
          title="No scans yet"
          description="Trigger a scan from the local agent (or click Run scan above)."
        />
      ) : (
        <>
          <div className="mb-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <section className="card">
              <h2 className="mb-3 text-sm font-semibold">Language distribution</h2>
              <ResponsiveContainer width="100%" height={240}>
                <PieChart>
                  <Pie data={languageDistribution} dataKey="value" nameKey="name" outerRadius={90}>
                    {languageDistribution.map((_, i) => (
                      <Cell key={i} fill={COLORS[i % COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            </section>
            <section className="card">
              <h2 className="mb-3 text-sm font-semibold">Top files by function count</h2>
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={topFunctions}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="name" interval={0} angle={-30} textAnchor="end" height={60} />
                  <YAxis allowDecimals={false} />
                  <Tooltip />
                  <Bar dataKey="count" fill="#5b71ff" />
                </BarChart>
              </ResponsiveContainer>
            </section>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_1fr]">
            <section className="card p-0">
              <h2 className="border-b border-slate-100 px-4 py-2 text-sm font-semibold">
                Files ({files.length})
              </h2>
              <ul className="max-h-[460px] overflow-y-auto text-sm">
                {files.map((f) => (
                  <li
                    key={f.id}
                    className={`cursor-pointer border-b border-slate-100 px-4 py-2 hover:bg-slate-50 ${
                      selected?.id === f.id ? 'bg-brand-50' : ''
                    }`}
                    onClick={() => setSelected(f)}
                  >
                    <div className="font-medium">{f.file_path}</div>
                    <div className="text-xs text-slate-500">
                      {f.language} · {f.functions_count} fn
                    </div>
                  </li>
                ))}
              </ul>
            </section>
            <section className="card">
              {selected ? (
                <>
                  <h2 className="text-sm font-semibold">{selected.file_path}</h2>
                  <p className="mt-1 text-xs text-slate-500">
                    {selected.language} · {selected.functions_count} functions
                  </p>
                  <p className="mt-3 text-sm">
                    {selected.semantic_description ?? 'No semantic description yet.'}
                  </p>
                  <h3 className="mt-4 text-xs font-semibold uppercase text-slate-500">Imports</h3>
                  <ul className="mt-1 flex flex-wrap gap-1">
                    {(selected.imports ?? []).map((imp) => (
                      <li key={imp} className="tag bg-slate-100 text-slate-700">
                        {imp}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="text-sm text-slate-500">Select a file to see details.</p>
              )}
            </section>
          </div>
        </>
      )}
    </>
  );
}
