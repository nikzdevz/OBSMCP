import { useState } from 'react';
import { ChevronsUpDown, FolderGit2, Plus } from 'lucide-react';

import { useProjects } from '../hooks/useProjects';
import { useProjectStore } from '../stores/project';
import CreateProjectModal from './CreateProjectModal';

export default function ProjectSelector(): JSX.Element {
  const { data: projects, isLoading } = useProjects();
  const currentId = useProjectStore((s) => s.currentProjectId);
  const setCurrent = useProjectStore((s) => s.setCurrentProjectId);
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);

  const current = projects?.find((p) => p.id === currentId) ?? null;

  return (
    <div className="relative border-b border-slate-200 px-3 py-3">
      <button
        type="button"
        className="flex w-full items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-2 py-2 text-left text-sm hover:bg-slate-100"
        onClick={() => setOpen((v) => !v)}
      >
        <FolderGit2 size={16} className="text-brand-600" />
        <span className="flex-1 truncate">
          {isLoading ? 'Loading…' : current?.name ?? 'Select project'}
        </span>
        <ChevronsUpDown size={14} className="text-slate-400" />
      </button>
      {open && (
        <div className="absolute left-3 right-3 top-full z-10 mt-1 max-h-72 overflow-auto rounded-md border border-slate-200 bg-white shadow-lg">
          {(projects ?? []).map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => {
                setCurrent(p.id);
                setOpen(false);
              }}
              className={`block w-full truncate px-3 py-2 text-left text-sm hover:bg-slate-100 ${
                p.id === currentId ? 'bg-brand-50 text-brand-700' : ''
              }`}
            >
              <div className="truncate font-medium">{p.name}</div>
              <div className="truncate text-xs text-slate-500">{p.path}</div>
            </button>
          ))}
          {(projects ?? []).length === 0 && (
            <div className="px-3 py-2 text-xs text-slate-500">No projects yet.</div>
          )}
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              setCreating(true);
            }}
            className="flex w-full items-center gap-2 border-t border-slate-100 px-3 py-2 text-left text-sm text-brand-700 hover:bg-brand-50"
          >
            <Plus size={14} /> New project
          </button>
        </div>
      )}
      {creating && <CreateProjectModal onClose={() => setCreating(false)} />}
    </div>
  );
}
