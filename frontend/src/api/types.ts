export interface Task {
  id: string;
  project_id: string | null;
  title: string;
  description: string | null;
  status: 'open' | 'in_progress' | 'done' | 'blocked';
  priority: 'low' | 'medium' | 'high' | 'urgent';
  tags: string[] | null;
  created_at: string;
  updated_at: string;
}

export interface Session {
  id: string;
  project_id: string | null;
  agent_id: string;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  context: string | null;
}

export interface Blocker {
  id: string;
  project_id: string | null;
  agent_id: string | null;
  description: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  status: 'active' | 'resolved';
  resolved_at: string | null;
  resolution: string | null;
  created_at: string;
}

export interface Decision {
  id: string;
  project_id: string | null;
  agent_id: string | null;
  decision: string;
  context: string | null;
  outcome: string | null;
  tags: string[] | null;
  created_at: string;
}

export interface WorkLog {
  id: string;
  project_id: string | null;
  session_id: string | null;
  agent_id: string | null;
  description: string;
  hours: number | null;
  tags: string[] | null;
  created_at: string;
}

export interface CodeAtlasScan {
  id: string;
  project_id: string | null;
  agent_id: string | null;
  status: 'pending' | 'running' | 'completed' | 'failed';
  total_files: number;
  scanned_files: number;
  started_at: string;
  completed_at: string | null;
  error_message: string | null;
}

export interface CodeAtlasFile {
  id: string;
  scan_id: string;
  project_id: string | null;
  file_path: string;
  language: string | null;
  functions_count: number;
  imports: string[];
  exports: string[];
  semantic_description: string | null;
  scanned_at: string;
}

export interface KnowledgeNode {
  id: string;
  project_id: string | null;
  agent_id: string | null;
  node_type: string;
  name: string;
  description: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface KnowledgeEdge {
  id: string;
  project_id: string | null;
  from_node_id: string;
  to_node_id: string;
  edge_type: string;
  weight: number;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface PerformanceLog {
  id: string;
  project_id: string | null;
  agent_id: string | null;
  session_id: string | null;
  metric_name: string;
  metric_value: number;
  unit: string | null;
  tags: Record<string, unknown>;
  logged_at: string;
}

export interface Stats {
  tasks: { total: number; open: number; in_progress: number; blocked: number; done: number };
  sessions: { total: number; active: number };
  blockers: { active: number; resolved: number };
  decisions: number;
  work_logs: number;
  nodes: number;
  edges: number;
  agents: number;
}
