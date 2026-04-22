/**
 * WorkflowDetail — visual workflow builder at /workflows/:id
 *
 * Layout:
 *   ┌─────────────────────────────────────────────┐
 *   │  Top bar: name | status badge | v# | buttons │
 *   ├────────────────────────┬────────────────────┤
 *   │  React Flow canvas     │  Property panel     │
 *   └────────────────────────┴────────────────────┘
 *
 * The workflow definition JSON (§6.1) uses a nodes-as-object shape:
 *   { version, entry, nodes: { [nodeId]: { type, ...config } } }
 *
 * We convert that ↔ ReactFlow arrays on load/save.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactFlow, {
  addEdge,
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";

import {
  activateWorkflow,
  archiveWorkflow,
  getWorkflow,
  pauseWorkflow,
  updateWorkflow,
} from "@/api/workflows";
import { listCampaigns } from "@/api/campaigns";
import type { WorkflowUpdate } from "@/api/types";
import { Badge, statusVariant } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Textarea } from "@/components/ui/Textarea";

// ---------------------------------------------------------------------------
// Types for the workflow definition (§6.1)
// ---------------------------------------------------------------------------
type NodeType = "entry" | "send" | "wait" | "branch" | "exit";

interface BaseNode {
  type: NodeType;
}
interface EntryNode extends BaseNode {
  type: "entry";
  next: string;
}
interface SendNode extends BaseNode {
  type: "send";
  campaign_id: string;
  next: string;
}
interface WaitNode extends BaseNode {
  type: "wait";
  duration: string; // ISO 8601
  next: string;
}
interface BranchNode extends BaseNode {
  type: "branch";
  condition: Record<string, unknown>;
  if_true: string;
  if_false: string;
}
interface ExitNode extends BaseNode {
  type: "exit";
  reason: string;
}
type WfNode = EntryNode | SendNode | WaitNode | BranchNode | ExitNode;

interface WorkflowDefinition {
  version: number;
  entry: string;
  nodes: Record<string, WfNode>;
}

// ---------------------------------------------------------------------------
// Conversion helpers: definition JSON ↔ ReactFlow nodes/edges
// ---------------------------------------------------------------------------
const NODE_WIDTH = 180;
const NODE_HEIGHT = 60;
const H_GAP = 220;
const V_GAP = 120;

/** Simple topological layout: BFS from entry, place nodes in columns */
function layoutNodes(
  def: WorkflowDefinition,
): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  const visited = new Set<string>();
  // BFS queue: [nodeId, col, row]
  const queue: [string, number, number][] = [[def.entry, 0, 0]];
  const colRows = new Map<number, number>(); // track next row per column

  while (queue.length > 0) {
    const item = queue.shift();
    if (!item) break;
    const [id, col, row] = item;
    if (visited.has(id)) continue;
    visited.add(id);

    const actualRow = colRows.get(col) ?? row;
    positions.set(id, { x: col * H_GAP, y: actualRow * V_GAP });
    colRows.set(col, actualRow + 1);

    const node = def.nodes[id];
    if (!node) continue;

    if (node.type === "branch") {
      const nextCol = col + 1;
      queue.push([node.if_true, nextCol, actualRow]);
      queue.push([node.if_false, nextCol, actualRow + 1]);
    } else if (node.type !== "exit") {
      queue.push([(node as EntryNode | SendNode | WaitNode).next, col + 1, actualRow]);
    }
  }

  // Any nodes not reachable from entry: place at bottom
  let orphanRow = positions.size;
  for (const id of Object.keys(def.nodes)) {
    if (!positions.has(id)) {
      positions.set(id, { x: 0, y: orphanRow * V_GAP });
      orphanRow++;
    }
  }

  return positions;
}

function definitionToFlow(def: WorkflowDefinition): {
  nodes: Node[];
  edges: Edge[];
} {
  const positions = layoutNodes(def);
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  for (const [id, node] of Object.entries(def.nodes)) {
    const pos = positions.get(id) ?? { x: 0, y: 0 };
    nodes.push({
      id,
      type: node.type,
      position: pos,
      data: { ...node, id },
      style: { width: NODE_WIDTH, height: NODE_HEIGHT },
    });

    if (node.type === "branch") {
      edges.push({
        id: `${id}->true`,
        source: id,
        target: node.if_true,
        sourceHandle: "if_true",
        label: "true",
        animated: false,
      });
      edges.push({
        id: `${id}->false`,
        source: id,
        target: node.if_false,
        sourceHandle: "if_false",
        label: "false",
        animated: false,
      });
    } else if (node.type !== "exit") {
      const n = node as EntryNode | SendNode | WaitNode;
      edges.push({
        id: `${id}->${n.next}`,
        source: id,
        target: n.next,
      });
    }
  }

  return { nodes, edges };
}

function flowToDefinition(
  rfNodes: Node[],
  rfEdges: Edge[],
  currentDef: WorkflowDefinition,
): WorkflowDefinition {
  const nodes: Record<string, WfNode> = {};

  for (const rfNode of rfNodes) {
    const { id, data } = rfNode;
    const type: NodeType = data.type;

    // Find edges from this node
    const outEdges = rfEdges.filter((e) => e.source === id);

    if (type === "entry") {
      const next = outEdges[0]?.target ?? "";
      nodes[id] = { type: "entry", next };
    } else if (type === "send") {
      const next = outEdges[0]?.target ?? "";
      nodes[id] = { type: "send", campaign_id: data.campaign_id ?? "", next };
    } else if (type === "wait") {
      const next = outEdges[0]?.target ?? "";
      nodes[id] = { type: "wait", duration: data.duration ?? "P1D", next };
    } else if (type === "branch") {
      const trueEdge = outEdges.find((e) => e.sourceHandle === "if_true");
      const falseEdge = outEdges.find((e) => e.sourceHandle === "if_false");
      nodes[id] = {
        type: "branch",
        condition: data.condition ?? {},
        if_true: trueEdge?.target ?? "",
        if_false: falseEdge?.target ?? "",
      };
    } else if (type === "exit") {
      nodes[id] = { type: "exit", reason: data.reason ?? "completed" };
    }
  }

  return {
    version: currentDef.version,
    entry: currentDef.entry,
    nodes,
  };
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------
function validateDefinition(
  def: WorkflowDefinition,
  campaignIds: Set<string>,
): string[] {
  const errors: string[] = [];
  const nodeIds = new Set(Object.keys(def.nodes));

  // Entry node exists
  if (!nodeIds.has(def.entry)) {
    errors.push(`Entry node "${def.entry}" does not exist.`);
  }

  for (const [id, node] of Object.entries(def.nodes)) {
    if (node.type === "send") {
      if (!node.campaign_id) {
        errors.push(`Send node "${id}" has no campaign_id.`);
      } else if (campaignIds.size > 0 && !campaignIds.has(node.campaign_id)) {
        errors.push(
          `Send node "${id}" references unknown campaign "${node.campaign_id}".`,
        );
      }
      if (!node.next || !nodeIds.has(node.next)) {
        errors.push(`Send node "${id}" next="${node.next}" is not a valid node.`);
      }
    } else if (node.type === "entry") {
      if (!node.next || !nodeIds.has(node.next)) {
        errors.push(
          `Entry node "${id}" next="${node.next}" is not a valid node.`,
        );
      }
    } else if (node.type === "wait") {
      if (!node.duration) {
        errors.push(`Wait node "${id}" has no duration.`);
      }
      if (!node.next || !nodeIds.has(node.next)) {
        errors.push(`Wait node "${id}" next="${node.next}" is not a valid node.`);
      }
    } else if (node.type === "branch") {
      if (!node.if_true || !nodeIds.has(node.if_true)) {
        errors.push(
          `Branch node "${id}" if_true="${node.if_true}" is not a valid node.`,
        );
      }
      if (!node.if_false || !nodeIds.has(node.if_false)) {
        errors.push(
          `Branch node "${id}" if_false="${node.if_false}" is not a valid node.`,
        );
      }
    }
  }

  // Acyclicity check via DFS
  const color = new Map<string, "white" | "gray" | "black">();
  for (const id of nodeIds) color.set(id, "white");

  function dfs(id: string): boolean {
    const c = color.get(id);
    if (c === "black") return false;
    if (c === "gray") return true; // cycle
    color.set(id, "gray");
    const node = def.nodes[id];
    if (!node) {
      color.set(id, "black");
      return false;
    }
    let hasCycle = false;
    if (node.type === "branch") {
      hasCycle = dfs(node.if_true) || dfs(node.if_false);
    } else if (node.type !== "exit") {
      hasCycle = dfs((node as EntryNode | SendNode | WaitNode).next);
    }
    color.set(id, "black");
    return hasCycle;
  }

  if (nodeIds.has(def.entry) && dfs(def.entry)) {
    errors.push("Workflow graph contains a cycle.");
  }

  return errors;
}

// ---------------------------------------------------------------------------
// Custom node components
// ---------------------------------------------------------------------------
const nodeStyle: React.CSSProperties = {
  padding: "8px 12px",
  borderRadius: 6,
  fontSize: 12,
  display: "flex",
  flexDirection: "column",
  justifyContent: "center",
  alignItems: "flex-start",
  width: NODE_WIDTH,
  minHeight: NODE_HEIGHT,
  boxSizing: "border-box",
  cursor: "pointer",
  border: "2px solid transparent",
};

function NodeShell({
  children,
  color,
  selected,
}: {
  children: React.ReactNode;
  color: string;
  selected: boolean;
}) {
  return (
    <div
      style={{
        ...nodeStyle,
        background: color,
        borderColor: selected ? "#6366f1" : "transparent",
        boxShadow: selected ? "0 0 0 2px #6366f133" : "0 1px 3px rgba(0,0,0,0.12)",
      }}
    >
      {children}
    </div>
  );
}

function EntryNodeComponent({ data, selected }: NodeProps) {
  return (
    <NodeShell color="#dbeafe" selected={!!selected}>
      <Handle type="source" position={Position.Right} />
      <div className="font-semibold text-blue-800">Entry</div>
      <div className="text-blue-600 text-xs truncate">{data.id}</div>
    </NodeShell>
  );
}

function SendNodeComponent({ data, selected }: NodeProps) {
  return (
    <NodeShell color="#dcfce7" selected={!!selected}>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      <div className="font-semibold text-green-800">Send</div>
      <div className="text-green-600 text-xs truncate">
        {data.campaign_id || <span className="italic opacity-60">no campaign</span>}
      </div>
    </NodeShell>
  );
}

function WaitNodeComponent({ data, selected }: NodeProps) {
  return (
    <NodeShell color="#fef9c3" selected={!!selected}>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      <div className="font-semibold text-yellow-800">Wait</div>
      <div className="text-yellow-600 text-xs">{data.duration || "—"}</div>
    </NodeShell>
  );
}

function BranchNodeComponent({ data, selected }: NodeProps) {
  return (
    <NodeShell color="#fce7f3" selected={!!selected}>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} id="if_true" style={{ top: "30%" }} />
      <Handle type="source" position={Position.Right} id="if_false" style={{ top: "70%" }} />
      <div className="font-semibold text-pink-800">Branch</div>
      <div className="text-pink-600 text-xs truncate">
        {data.condition?.kind ?? "—"}
      </div>
    </NodeShell>
  );
}

function ExitNodeComponent({ data, selected }: NodeProps) {
  return (
    <NodeShell color="#f3f4f6" selected={!!selected}>
      <Handle type="target" position={Position.Left} />
      <div className="font-semibold text-gray-700">Exit</div>
      <div className="text-gray-500 text-xs">{data.reason || "completed"}</div>
    </NodeShell>
  );
}

const nodeTypes = {
  entry: EntryNodeComponent,
  send: SendNodeComponent,
  wait: WaitNodeComponent,
  branch: BranchNodeComponent,
  exit: ExitNodeComponent,
};

// ---------------------------------------------------------------------------
// Property panel
// ---------------------------------------------------------------------------
interface PropertyPanelProps {
  node: Node | null;
  onChange: (id: string, patch: Record<string, unknown>) => void;
  campaigns: Array<{ id: string; name: string }>;
}

function PropertyPanel({ node, onChange, campaigns }: PropertyPanelProps) {
  if (!node) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground p-4">
        Select a node to edit its properties
      </div>
    );
  }

  const { id, data } = node;
  const type: NodeType = data.type;

  const patch = (field: string, value: unknown) => onChange(id, { [field]: value });

  const campaignOptions = [
    { label: "— select campaign —", value: "" },
    ...campaigns.map((c) => ({ label: c.name, value: c.id })),
  ];

  return (
    <div className="space-y-4 p-4">
      <div>
        <div className="text-xs text-muted-foreground mb-1">Node ID</div>
        <div className="font-mono text-sm bg-muted/40 rounded px-2 py-1">{id}</div>
      </div>

      <div>
        <div className="text-xs text-muted-foreground mb-1">Type</div>
        <div className="text-sm font-medium capitalize">{type}</div>
      </div>

      {type === "entry" && (
        <div className="text-sm text-muted-foreground italic">
          Entry node — no additional config.
        </div>
      )}

      {type === "send" && (
        <div className="space-y-2">
          <label className="text-sm font-medium">Campaign</label>
          {campaigns.length > 0 ? (
            <Select
              options={campaignOptions}
              value={data.campaign_id ?? ""}
              onChange={(v) => patch("campaign_id", v)}
            />
          ) : (
            <Input
              value={data.campaign_id ?? ""}
              onChange={(e) => patch("campaign_id", e.target.value)}
              placeholder="Campaign UUID"
            />
          )}
        </div>
      )}

      {type === "wait" && (
        <div className="space-y-2">
          <label className="text-sm font-medium">Duration (ISO 8601)</label>
          <Input
            value={data.duration ?? ""}
            onChange={(e) => patch("duration", e.target.value)}
            placeholder="P3D or PT6H"
          />
          <div className="text-xs text-muted-foreground">
            Examples: P1D (1 day), P3D (3 days), PT6H (6 hours), P1W (1 week)
          </div>
        </div>
      )}

      {type === "branch" && (
        <div className="space-y-2">
          <label className="text-sm font-medium">Condition (JSON)</label>
          <Textarea
            className="min-h-[120px] font-mono text-xs"
            value={
              typeof data.condition === "object"
                ? JSON.stringify(data.condition, null, 2)
                : String(data.condition ?? "")
            }
            onChange={(e) => {
              try {
                patch("condition", JSON.parse(e.target.value));
              } catch {
                // Allow partial editing — only commit valid JSON
              }
            }}
          />
          <div className="text-xs text-muted-foreground space-y-1">
            <div>kind: event_in_window | click_url_match | and | or</div>
            <div>event_type: open | click | any_engagement</div>
          </div>
        </div>
      )}

      {type === "exit" && (
        <div className="space-y-2">
          <label className="text-sm font-medium">Reason</label>
          <Input
            value={data.reason ?? "completed"}
            onChange={(e) => patch("reason", e.target.value)}
            placeholder="completed"
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add-node toolbar
// ---------------------------------------------------------------------------
type AddNodeType = "send" | "wait" | "branch" | "exit";

const ADD_NODE_TYPES: { label: string; type: AddNodeType }[] = [
  { label: "+ Send", type: "send" },
  { label: "+ Wait", type: "wait" },
  { label: "+ Branch", type: "branch" },
  { label: "+ Exit", type: "exit" },
];

function makeNodeId(type: string): string {
  return `node_${type}_${Math.random().toString(36).slice(2, 7)}`;
}

function defaultNodeData(type: AddNodeType): Record<string, unknown> {
  if (type === "send") return { type, campaign_id: "", next: "" };
  if (type === "wait") return { type, duration: "P1D", next: "" };
  if (type === "branch") {
    return {
      type,
      condition: { kind: "event_in_window", event_type: "open", since_node: "", window: "P3D" },
      if_true: "",
      if_false: "",
    };
  }
  return { type, reason: "completed" };
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export function WorkflowDetail() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();

  const [rfNodes, setRfNodes, onNodesChange] = useNodesState([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState([]);
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);
  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [saveErrors, setSaveErrors] = useState<string[]>([]);
  const [successMsg, setSuccessMsg] = useState("");
  const currentDefRef = useRef<WorkflowDefinition>({
    version: 1,
    entry: "node_start",
    nodes: {},
  });

  // Load campaigns for the property panel send node picker
  const campaignsQuery = useQuery({
    queryKey: ["campaigns-brief"],
    queryFn: () => listCampaigns({ limit: 500 }),
  });
  const campaigns = campaignsQuery.data?.campaigns ?? [];

  const workflowQuery = useQuery({
    queryKey: ["workflow", id],
    queryFn: () => getWorkflow(id!),
    enabled: !!id,
  });

  // When workflow loads, convert definition → ReactFlow nodes/edges
  useEffect(() => {
    const wf = workflowQuery.data;
    if (!wf) return;
    setDraftName(wf.name);
    const def = wf.definition as unknown as WorkflowDefinition;
    currentDefRef.current = def;
    const { nodes, edges } = definitionToFlow(def);
    setRfNodes(nodes);
    setRfEdges(edges);
    setSaveErrors([]);
    setSuccessMsg("");
  }, [workflowQuery.data, setRfNodes, setRfEdges]);

  const updateMut = useMutation({
    mutationFn: (patch: WorkflowUpdate) => updateWorkflow(id!, patch),
    onSuccess: (wf) => {
      qc.setQueryData(["workflow", id], wf);
      qc.invalidateQueries({ queryKey: ["workflows"] });
      setSuccessMsg("Saved");
      setTimeout(() => setSuccessMsg(""), 2000);
    },
    onError: (e) => {
      setSaveErrors([(e as Error).message]);
    },
  });

  const activateMut = useMutation({
    mutationFn: () => activateWorkflow(id!),
    onSuccess: (wf) => {
      qc.setQueryData(["workflow", id], wf);
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  const pauseMut = useMutation({
    mutationFn: () => pauseWorkflow(id!),
    onSuccess: (wf) => {
      qc.setQueryData(["workflow", id], wf);
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  const archiveMut = useMutation({
    mutationFn: () => archiveWorkflow(id!),
    onSuccess: (wf) => {
      qc.setQueryData(["workflow", id], wf);
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  const campaignIds = useMemo(
    () => new Set(campaigns.map((c) => c.id)),
    [campaigns],
  );

  const handleSave = useCallback(() => {
    setSaveErrors([]);
    const def = flowToDefinition(rfNodes, rfEdges, currentDefRef.current);
    const errors = validateDefinition(def, campaignIds);
    if (errors.length > 0) {
      setSaveErrors(errors);
      return;
    }
    const patch: WorkflowUpdate = {
      definition: def as unknown as Record<string, unknown>,
    };
    if (draftName !== workflowQuery.data?.name) {
      patch.name = draftName;
    }
    updateMut.mutate(patch);
  }, [rfNodes, rfEdges, draftName, workflowQuery.data?.name, campaignIds, updateMut]);

  const onConnect = useCallback(
    (params: Connection) => setRfEdges((eds) => addEdge(params, eds)),
    [setRfEdges],
  );

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    setSelectedNode(node);
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
  }, []);

  const handleNodeDataChange = useCallback(
    (nodeId: string, patch: Record<string, unknown>) => {
      setRfNodes((nds) =>
        nds.map((n) =>
          n.id === nodeId ? { ...n, data: { ...n.data, ...patch } } : n,
        ),
      );
      setSelectedNode((prev) =>
        prev?.id === nodeId ? { ...prev, data: { ...prev.data, ...patch } } : prev,
      );
    },
    [setRfNodes],
  );

  const handleAddNode = useCallback(
    (type: AddNodeType) => {
      const newId = makeNodeId(type);
      const data = { ...defaultNodeData(type), id: newId };
      const newNode: Node = {
        id: newId,
        type,
        position: { x: 100 + Math.random() * 200, y: 100 + Math.random() * 200 },
        data,
        style: { width: NODE_WIDTH, height: NODE_HEIGHT },
      };
      setRfNodes((nds) => [...nds, newNode]);
    },
    [setRfNodes],
  );

  const handleDeleteSelected = useCallback(() => {
    if (!selectedNode) return;
    setRfNodes((nds) => nds.filter((n) => n.id !== selectedNode.id));
    setRfEdges((eds) =>
      eds.filter(
        (e) => e.source !== selectedNode.id && e.target !== selectedNode.id,
      ),
    );
    setSelectedNode(null);
  }, [selectedNode, setRfNodes, setRfEdges]);

  if (!id) return <div>Missing workflow id</div>;
  if (workflowQuery.isLoading) return <div>Loading...</div>;
  if (workflowQuery.error) {
    return (
      <div className="text-destructive">
        {(workflowQuery.error as Error).message}
      </div>
    );
  }

  const wf = workflowQuery.data;
  if (!wf) return <div>Not found</div>;

  const wfStatus = wf.status;
  const canActivate = wfStatus === "draft" || wfStatus === "paused";
  const canPause = wfStatus === "active";
  const canArchive = wfStatus !== "archived";
  const isReadOnly = wfStatus === "archived";
  const anyPending =
    updateMut.isPending ||
    activateMut.isPending ||
    pauseMut.isPending ||
    archiveMut.isPending;
  const mutError =
    activateMut.error || pauseMut.error || archiveMut.error
      ? ((activateMut.error || pauseMut.error || archiveMut.error) as Error)
          .message
      : null;

  return (
    <div className="flex flex-col h-[calc(100vh-4rem)]">
      {/* Top bar */}
      <div className="flex items-center gap-3 px-4 py-2 border-b border-border bg-card flex-shrink-0 flex-wrap">
        <Link
          to="/workflows"
          className="text-sm text-muted-foreground hover:underline flex-shrink-0"
        >
          ← Workflows
        </Link>

        {/* Name */}
        {editingName ? (
          <Input
            className="h-8 w-64 text-sm"
            value={draftName}
            autoFocus
            onChange={(e) => setDraftName(e.target.value)}
            onBlur={() => setEditingName(false)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setEditingName(false);
              if (e.key === "Escape") {
                setDraftName(wf.name);
                setEditingName(false);
              }
            }}
          />
        ) : (
          <button
            className="text-base font-semibold hover:underline decoration-dotted cursor-pointer"
            onClick={() => !isReadOnly && setEditingName(true)}
            title={isReadOnly ? undefined : "Click to rename"}
          >
            {draftName}
          </button>
        )}

        <Badge variant={statusVariant(wfStatus)}>{wfStatus}</Badge>
        <span className="text-xs text-muted-foreground">v{wf.version}</span>

        <div className="flex-1" />

        {/* Action buttons */}
        <div className="flex items-center gap-2 flex-wrap">
          {!isReadOnly && (
            <Button size="sm" onClick={handleSave} disabled={anyPending}>
              {updateMut.isPending ? "Saving..." : "Save"}
            </Button>
          )}
          {canActivate && (
            <Button
              size="sm"
              onClick={() => activateMut.mutate()}
              disabled={anyPending}
            >
              Activate
            </Button>
          )}
          {canPause && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => pauseMut.mutate()}
              disabled={anyPending}
            >
              Pause
            </Button>
          )}
          {canArchive && (
            <Button
              size="sm"
              variant="destructive"
              onClick={() => archiveMut.mutate()}
              disabled={anyPending}
            >
              Archive
            </Button>
          )}
        </div>
      </div>

      {/* Error / success banners */}
      {saveErrors.length > 0 && (
        <div className="bg-destructive/10 border-b border-destructive/20 px-4 py-2 text-sm text-destructive flex-shrink-0">
          <strong>Validation errors:</strong>
          <ul className="list-disc pl-5 mt-1">
            {saveErrors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {mutError && (
        <div className="bg-destructive/10 border-b border-destructive/20 px-4 py-2 text-sm text-destructive flex-shrink-0">
          {mutError}
        </div>
      )}
      {successMsg && (
        <div className="bg-green-50 border-b border-green-200 px-4 py-2 text-sm text-green-700 flex-shrink-0">
          {successMsg}
        </div>
      )}

      {/* Canvas + panel */}
      <div className="flex flex-1 min-h-0">
        {/* Left toolbar */}
        <div className="flex flex-col gap-2 p-2 border-r border-border bg-card flex-shrink-0 w-24">
          <div className="text-xs text-muted-foreground font-medium px-1 py-1">
            Add node
          </div>
          {ADD_NODE_TYPES.map(({ label, type }) => (
            <button
              key={type}
              disabled={isReadOnly}
              onClick={() => handleAddNode(type)}
              className="text-xs rounded border border-border px-2 py-1.5 hover:bg-accent disabled:opacity-50 text-left"
            >
              {label}
            </button>
          ))}
          {selectedNode && !isReadOnly && (
            <>
              <div className="mt-2 border-t border-border pt-2" />
              <button
                onClick={handleDeleteSelected}
                className="text-xs rounded border border-destructive/40 px-2 py-1.5 text-destructive hover:bg-destructive/10 text-left"
              >
                Delete
              </button>
            </>
          )}
        </div>

        {/* React Flow canvas */}
        <div className="flex-1 min-w-0">
          <ReactFlow
            nodes={rfNodes}
            edges={rfEdges}
            nodeTypes={nodeTypes}
            onNodesChange={isReadOnly ? undefined : onNodesChange}
            onEdgesChange={isReadOnly ? undefined : onEdgesChange}
            onConnect={isReadOnly ? undefined : onConnect}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            deleteKeyCode={null}
          >
            <Background />
            <Controls />
            <MiniMap
              nodeColor={(n) => {
                const t = n.type ?? "";
                if (t === "entry") return "#bfdbfe";
                if (t === "send") return "#bbf7d0";
                if (t === "wait") return "#fef08a";
                if (t === "branch") return "#fbcfe8";
                return "#e5e7eb";
              }}
            />
          </ReactFlow>
        </div>

        {/* Right property panel */}
        <div className="w-72 border-l border-border bg-card overflow-y-auto flex-shrink-0">
          <div className="text-xs font-medium text-muted-foreground px-4 py-2 border-b border-border">
            Properties
          </div>
          {isReadOnly ? (
            <div className="p-4 text-sm text-muted-foreground italic">
              Workflow is archived — read only.
            </div>
          ) : (
            <PropertyPanel
              node={selectedNode}
              onChange={handleNodeDataChange}
              campaigns={campaigns}
            />
          )}
        </div>
      </div>
    </div>
  );
}
