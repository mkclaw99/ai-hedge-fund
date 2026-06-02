// One forecaster canvas node may run multiple foundation-model backbones
// (Chronos-2 and/or Toto-2.0). The backend's graph builder still treats
// each backbone as its own agent — the prefix on the ``agent_id`` decides
// which model loads (see ``src/agents/forecaster.py::_resolve_backbone``).
// So at request-build time we expand a single ``forecaster-node`` into
// one ``graph_nodes`` entry per selected backbone, and duplicate any
// edges that touch it so the upstream/downstream prose flow reaches both
// backbone agents.
//
// Legacy two-node flows (``forecaster_*`` + ``toto_forecaster_*`` on the
// same canvas) keep working: each canvas node defaults to its own
// single-backbone selection and the expansion is a no-op for them.

import { getNodeInternalState } from '@/hooks/use-node-state';

type BackboneId = 'chronos2' | 'toto2';

interface GraphNodeLike {
  id: string;
  type?: string;
  data: unknown;
  position: { x: number; y: number };
}

interface GraphEdgeLike {
  source: string;
  target: string;
  [k: string]: unknown;
}

// Strip whichever forecaster prefix is present so we can rebuild both
// per-backbone agent_ids from the canvas node's suffix.
function canvasSuffix(canvasId: string): string {
  return canvasId.replace(/^toto_forecaster_|^forecaster_/, '');
}

function agentIdFor(canvasId: string, b: BackboneId): string {
  const suffix = canvasSuffix(canvasId);
  return b === 'toto2' ? `toto_forecaster_${suffix}` : `forecaster_${suffix}`;
}

// Read the user's backbones selection out of the canvas node's
// internal_state. Legacy canvas ids fall back to a single-backbone
// default that matches their old behaviour, so flows from before the
// multi-select PR keep producing exactly the same backend runs.
function backbonesForNode(canvasId: string): BackboneId[] {
  const st = (getNodeInternalState(canvasId) as Record<string, unknown> | undefined) || {};
  const saved = st.forecasterBackbones;
  if (Array.isArray(saved) && saved.length > 0) {
    return saved.filter((x): x is BackboneId => x === 'chronos2' || x === 'toto2');
  }
  return canvasId.startsWith('toto_forecaster') ? ['toto2'] : ['chronos2'];
}

/**
 * Expand any ``forecaster-node`` canvas entries into one graph_nodes
 * entry per selected backbone. Returns BOTH the expanded nodes list and
 * a per-canvas-id mapping so edge expansion can fan out edges that
 * reference a forecaster canvas node.
 */
export function expandForecasterGraph<N extends GraphNodeLike, E extends GraphEdgeLike>(
  agentNodes: readonly N[],
  edges: readonly E[],
): { nodes: N[]; edges: E[] } {
  const expansionsByCanvasId = new Map<string, string[]>();
  const expandedNodes: N[] = [];
  for (const node of agentNodes) {
    if (node.type === 'forecaster-node') {
      const backbones = backbonesForNode(node.id);
      const synthIds = backbones.map((b) => agentIdFor(node.id, b));
      expansionsByCanvasId.set(node.id, synthIds);
      for (const sid of synthIds) {
        expandedNodes.push({ ...node, id: sid });
      }
    } else {
      expandedNodes.push(node);
    }
  }
  // Edge fan-out: if either endpoint was a forecaster canvas node, emit
  // an edge per (source-expansion, target-expansion) so upstream/downstream
  // wiring stays intact for both backbone agents.
  const expandedEdges: E[] = [];
  for (const edge of edges) {
    const sources = expansionsByCanvasId.get(edge.source) ?? [edge.source];
    const targets = expansionsByCanvasId.get(edge.target) ?? [edge.target];
    for (const s of sources) {
      for (const t of targets) {
        expandedEdges.push({ ...edge, source: s, target: t });
      }
    }
  }
  return { nodes: expandedNodes, edges: expandedEdges };
}
