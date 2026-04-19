import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '../api/client';
import type { Project } from '../api/types';
import { useProjectStore } from '../stores/project';

interface Props {
  onClose: () => void;
}

export default function CreateProjectModal({ onClose }: Props): JSX.Element {
  const qc = useQueryClient();
  const setCurrent = useProjectStore((s) => s.setCurrentProjectId);
  const [name, setName] = useState('');
  const [path, setPath] = useState('');
  const [repoUrl, setRepoUrl] = useState('');

  const create = useMutation({
    mutationFn: (body: { name: string; path: string; repo_url: string | null }) =>
      api.post<Project>('/api/projects', body),
    onSuccess: (project) => {
      qc.setQueryData<Project[]>(['projects'], (prev) =>
        prev ? [...prev, project] : [project],
      );
      setCurrent(project.id);
      qc.invalidateQueries({ queryKey: ['projects'] });
      onClose();
    },
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !path.trim()) return;
    create.mutate({
      name: name.trim(),
      path: path.trim(),
      repo_url: repoUrl.trim() || null,
    });
  };

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-slate-900/40 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl"
      >
        <h2 className="mb-3 text-lg font-semibold">New project</h2>
        <label className="mb-3 block text-sm">
          Name
          <input
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </label>
        <label className="mb-3 block text-sm">
          Path
          <input
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            placeholder="/home/you/projects/acme"
            value={path}
            onChange={(e) => setPath(e.target.value)}
          />
        </label>
        <label className="mb-4 block text-sm">
          Repo URL <span className="text-slate-400">(optional)</span>
          <input
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
          />
        </label>
        {create.isError && (
          <p className="mb-3 text-sm text-rose-600">
            {(create.error as Error).message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="rounded px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={create.isPending || !name.trim() || !path.trim()}
          >
            {create.isPending ? 'Creating…' : 'Create'}
          </button>
        </div>
      </form>
    </div>
  );
}
