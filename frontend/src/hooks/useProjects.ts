import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';

import { api } from '../api/client';
import type { Project } from '../api/types';
import { useProjectStore } from '../stores/project';

export function useProjects() {
  const setCurrent = useProjectStore((s) => s.setCurrentProjectId);
  const currentId = useProjectStore((s) => s.currentProjectId);
  const query = useQuery<Project[]>({
    queryKey: ['projects'],
    queryFn: () => api.get<Project[]>('/api/projects'),
  });

  useEffect(() => {
    const list = query.data;
    if (!list || list.length === 0) return;
    if (!currentId || !list.some((p) => p.id === currentId)) {
      setCurrent(list[0].id);
    }
  }, [query.data, currentId, setCurrent]);

  return query;
}
