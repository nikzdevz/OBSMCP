import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';

interface ProjectState {
  currentProjectId: string | null;
  setCurrentProjectId: (id: string | null) => void;
}

export const useProjectStore = create<ProjectState>()(
  persist(
    (set) => ({
      currentProjectId: null,
      setCurrentProjectId: (id) => set({ currentProjectId: id }),
    }),
    {
      name: 'obsmcp:project',
      storage: createJSONStorage(() => localStorage),
    },
  ),
);

export function useCurrentProjectId(): string | null {
  return useProjectStore((s) => s.currentProjectId);
}
