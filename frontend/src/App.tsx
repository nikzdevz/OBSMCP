import { useEffect } from 'react';
import { Route, Routes } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';

import Layout from './components/Layout';
import Dashboard from './pages/Dashboard';
import Tasks from './pages/Tasks';
import Sessions from './pages/Sessions';
import Blockers from './pages/Blockers';
import Decisions from './pages/Decisions';
import WorkLogs from './pages/WorkLogs';
import CodeAtlas from './pages/CodeAtlas';
import KnowledgeGraph from './pages/KnowledgeGraph';
import PerformanceLogs from './pages/PerformanceLogs';
import SettingsPage from './pages/Settings';
import NotFound from './pages/NotFound';
import { eventBus } from './events/EventBus';
import { useProjectStore } from './stores/project';

export default function App(): JSX.Element {
  const qc = useQueryClient();
  useEffect(() => {
    eventBus.attachQueryClient(qc);
    eventBus.setProjectIdProvider(() => useProjectStore.getState().currentProjectId);
    eventBus.connect();
    return () => eventBus.disconnect();
  }, [qc]);

  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="tasks" element={<Tasks />} />
        <Route path="sessions" element={<Sessions />} />
        <Route path="blockers" element={<Blockers />} />
        <Route path="decisions" element={<Decisions />} />
        <Route path="work-logs" element={<WorkLogs />} />
        <Route path="code-atlas" element={<CodeAtlas />} />
        <Route path="graph" element={<KnowledgeGraph />} />
        <Route path="logs" element={<PerformanceLogs />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
