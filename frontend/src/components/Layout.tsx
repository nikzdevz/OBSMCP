import { NavLink, Outlet } from 'react-router-dom';
import {
  Activity,
  AlertTriangle,
  BookOpenText,
  ClipboardList,
  Gauge,
  Layers,
  LineChart,
  ListChecks,
  Network,
  Settings,
  Wifi,
  WifiOff,
  type LucideIcon,
} from 'lucide-react';
import { useConnectionStatus } from '../hooks/useConnectionStatus';
import ProjectSelector from './ProjectSelector';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
}

const NAV: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: Gauge },
  { to: '/tasks', label: 'Tasks', icon: ListChecks },
  { to: '/sessions', label: 'Sessions', icon: Activity },
  { to: '/blockers', label: 'Blockers', icon: AlertTriangle },
  { to: '/decisions', label: 'Decisions', icon: BookOpenText },
  { to: '/work-logs', label: 'Work Logs', icon: ClipboardList },
  { to: '/code-atlas', label: 'Code Atlas', icon: Layers },
  { to: '/graph', label: 'Knowledge Graph', icon: Network },
  { to: '/logs', label: 'Performance', icon: LineChart },
  { to: '/settings', label: 'Settings', icon: Settings },
];

export default function Layout(): JSX.Element {
  const connected = useConnectionStatus();
  return (
    <div className="flex h-full">
      <aside className="flex w-60 flex-col border-r border-slate-200 bg-white">
        <div className="flex items-center gap-2 px-4 py-4 text-lg font-semibold">
          <span className="inline-block h-6 w-6 rounded-md bg-brand-600" />
          OBSMCP
        </div>
        <ProjectSelector />
        <nav className="flex-1 px-2 pb-4 pt-3">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition ${
                  isActive
                    ? 'bg-brand-50 text-brand-700'
                    : 'text-slate-700 hover:bg-slate-100'
                }`
              }
            >
              <item.icon size={18} />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="flex items-center justify-between border-t border-slate-200 px-4 py-3 text-xs text-slate-500">
          <span className="flex items-center gap-1.5">
            {connected ? <Wifi size={14} className="text-emerald-600" /> : <WifiOff size={14} className="text-rose-500" />}
            {connected ? 'Live' : 'Offline'}
          </span>
          <span>v0.1.0</span>
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto p-6">
        <Outlet />
      </main>
    </div>
  );
}
