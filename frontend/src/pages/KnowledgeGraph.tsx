import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import ReactFlow, { Background, Controls, MiniMap, Edge, Node } from 'reactflow';
import 'reactflow/dist/style.css';

import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import { api, buildQuery } from '../api/client';
import type { KnowledgeEdge, KnowledgeNode } from '../api/types';
import { useCurrentProjectId } from '../stores/project';

interface Graph {
  nodes: KnowledgeNode[];
  edges: KnowledgeEdge[];
}

export default function KnowledgeGraphPage(): JSX.Element {
  const projectId = useCurrentProjectId();
  const graph = useQuery<Graph>({
    queryKey: ['knowledge-graph', { projectId }],
    queryFn: () => api.get<Graph>(buildQuery('/api/knowledge-graph', { project_id: projectId })),
  });

  const { nodes, edges } = useMemo(() => {
    const g: Graph = graph.data ?? { nodes: [], edges: [] };
    const radius = Math.max(200, 32 * g.nodes.length);
    const rfNodes: Node[] = g.nodes.slice(0, 300).map((n, i) => {
      const angle = (i / Math.max(g.nodes.length, 1)) * 2 * Math.PI;
      return {
        id: n.id,
        position: { x: Math.cos(angle) * radius + radius, y: Math.sin(angle) * radius + radius },
        data: { label: `${n.node_type}: ${n.name}` },
        style: { padding: 6, borderRadius: 6, background: '#fff', border: '1px solid #cbd5e1' },
      };
    });
    const rfEdges: Edge[] = g.edges
      .slice(0, 500)
      .map((e) => ({ id: e.id, source: e.from_node_id, target: e.to_node_id, label: e.edge_type }));
    return { nodes: rfNodes, edges: rfEdges };
  }, [graph.data]);

  return (
    <>
      <PageHeader title="Knowledge Graph" description="Relationships between code & concepts." />
      {(graph.data?.nodes.length ?? 0) === 0 ? (
        <EmptyState title="Graph is empty" description="Add nodes via the MCP tool or bulk API." />
      ) : (
        <div className="card h-[calc(100vh-220px)] p-0">
          <ReactFlow nodes={nodes} edges={edges} fitView>
            <Background />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>
      )}
    </>
  );
}
