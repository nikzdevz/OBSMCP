import { QueryClient } from '@tanstack/react-query';
import { getApiToken } from '../api/client';

export type EventType =
  | 'connected'
  | 'heartbeat'
  | 'task_created'
  | 'task_updated'
  | 'task_deleted'
  | 'session_opened'
  | 'session_closed'
  | 'session_heartbeat'
  | 'blocker_logged'
  | 'blocker_resolved'
  | 'blocker_deleted'
  | 'decision_logged'
  | 'decision_updated'
  | 'decision_deleted'
  | 'work_logged'
  | 'work_log_updated'
  | 'work_log_deleted'
  | 'scan_started'
  | 'scan_progress'
  | 'scan_completed'
  | 'node_created'
  | 'node_updated'
  | 'node_deleted'
  | 'nodes_bulk_created'
  | 'edge_created'
  | 'edge_deleted'
  | 'edges_bulk_created'
  | 'perf_log_received'
  | 'agent_connected'
  | 'agent_disconnected';

export interface OBSMCPEvent {
  type: EventType | string;
  payload: Record<string, unknown>;
  timestamp: string;
}

type Listener = (event: OBSMCPEvent) => void;

const EVENT_TO_QUERY: Record<string, string[]> = {
  task_created: ['tasks', 'stats'],
  task_updated: ['tasks', 'stats'],
  task_deleted: ['tasks', 'stats'],
  session_opened: ['sessions', 'stats'],
  session_closed: ['sessions', 'stats'],
  session_heartbeat: ['sessions'],
  blocker_logged: ['blockers', 'stats'],
  blocker_resolved: ['blockers', 'stats'],
  blocker_deleted: ['blockers', 'stats'],
  decision_logged: ['decisions', 'stats'],
  decision_updated: ['decisions'],
  decision_deleted: ['decisions', 'stats'],
  work_logged: ['work-logs', 'stats'],
  work_log_updated: ['work-logs'],
  work_log_deleted: ['work-logs', 'stats'],
  scan_started: ['code-atlas'],
  scan_progress: ['code-atlas'],
  scan_completed: ['code-atlas'],
  node_created: ['knowledge-graph', 'stats'],
  node_updated: ['knowledge-graph'],
  node_deleted: ['knowledge-graph', 'stats'],
  nodes_bulk_created: ['knowledge-graph', 'stats'],
  edge_created: ['knowledge-graph', 'stats'],
  edge_deleted: ['knowledge-graph', 'stats'],
  edges_bulk_created: ['knowledge-graph', 'stats'],
  perf_log_received: ['performance-logs'],
  agent_connected: ['agents', 'stats'],
  agent_disconnected: ['agents', 'stats'],
};

export class EventBus {
  private listeners = new Set<Listener>();
  private es: EventSource | null = null;
  private reconnectTimer: number | null = null;
  private queryClient: QueryClient | null = null;
  public connected = false;
  private statusListeners = new Set<(c: boolean) => void>();

  attachQueryClient(qc: QueryClient): void {
    this.queryClient = qc;
  }

  connect(): void {
    if (this.es) return;
    const token = getApiToken();
    const url = token ? `/api/events?token=${encodeURIComponent(token)}` : '/api/events';
    const es = new EventSource(url);
    this.es = es;
    es.onopen = () => {
      this.connected = true;
      this.statusListeners.forEach((fn) => fn(true));
    };
    es.onerror = () => {
      this.connected = false;
      this.statusListeners.forEach((fn) => fn(false));
      es.close();
      this.es = null;
      this.scheduleReconnect();
    };
    es.onmessage = (ev) => this.handleMessage(ev.data);
    // Listen to all named events too.
    const knownTypes: string[] = Object.keys(EVENT_TO_QUERY).concat(['connected', 'heartbeat']);
    for (const t of knownTypes) {
      es.addEventListener(t, (ev: MessageEvent) => this.handleMessage(ev.data));
    }
  }

  disconnect(): void {
    if (this.es) {
      this.es.close();
      this.es = null;
    }
    if (this.reconnectTimer) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.connected = false;
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onStatus(fn: (connected: boolean) => void): () => void {
    this.statusListeners.add(fn);
    return () => this.statusListeners.delete(fn);
  }

  private handleMessage(raw: string): void {
    if (!raw || raw.startsWith(':')) return;
    let event: OBSMCPEvent;
    try {
      event = JSON.parse(raw);
    } catch {
      return;
    }
    this.listeners.forEach((l) => l(event));
    const keys = EVENT_TO_QUERY[event.type] ?? [];
    if (this.queryClient) {
      for (const k of keys) {
        void this.queryClient.invalidateQueries({ queryKey: [k] });
      }
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 3_000);
  }
}

export const eventBus = new EventBus();
