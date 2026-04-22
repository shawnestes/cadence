import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { createWorkflow, listWorkflows } from "@/api/workflows";
import type { WorkflowIn } from "@/api/types";
import { Badge, statusVariant } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Table, Tbody, Td, Th, Thead, Tr } from "@/components/ui/Table";

const PAGE_SIZE = 25;

const STATUS_OPTIONS = [
  { label: "All", value: "" },
  { label: "draft", value: "draft" },
  { label: "active", value: "active" },
  { label: "paused", value: "paused" },
  { label: "archived", value: "archived" },
];

const VERTICAL_OPTIONS_FILTER = [
  { label: "All", value: "" },
  { label: "auto", value: "auto" },
  { label: "home", value: "home" },
];

const VERTICAL_OPTIONS = [
  { label: "auto", value: "auto" },
  { label: "home", value: "home" },
];

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function Workflows() {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const [status, setStatus] = useState("");
  const [vertical, setVertical] = useState("");
  const [page, setPage] = useState(0);
  const [showForm, setShowForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [newVertical, setNewVertical] = useState("auto");

  const args = {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    ...(status ? { status } : {}),
    ...(vertical ? { vertical } : {}),
  };

  const { data, isLoading, error } = useQuery({
    queryKey: ["workflows", args],
    queryFn: () => listWorkflows(args),
  });

  const createMut = useMutation({
    mutationFn: (payload: WorkflowIn) => createWorkflow(payload),
    onSuccess: (wf) => {
      qc.invalidateQueries({ queryKey: ["workflows"] });
      navigate(`/workflows/${wf.id}`);
    },
  });

  const total = data?.total ?? 0;
  const rows = data?.workflows ?? [];
  const hasNext = (page + 1) * PAGE_SIZE < total;

  const onCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    createMut.mutate({
      name: newName.trim(),
      vertical: newVertical,
      definition: {
        version: 1,
        entry: "node_start",
        nodes: {
          node_start: { type: "entry", next: "node_exit" },
          node_exit: { type: "exit", reason: "completed" },
        },
      },
    });
  };

  return (
    <div className="space-y-6">
      {/* Masthead */}
      <div className="border-b border-border pb-4 flex items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Workflows</h1>
          <p className="mt-1 text-xs font-mono text-muted-foreground uppercase tracking-[0.1em]">
            Journey definitions & automation sequences
          </p>
        </div>
        <Button onClick={() => setShowForm((s) => !s)}>
          {showForm ? "Cancel" : "New Workflow"}
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <label className="text-xs font-mono uppercase tracking-[0.1em] text-muted-foreground">Status</label>
          <Select
            options={STATUS_OPTIONS}
            value={status}
            onChange={(v) => {
              setStatus(v);
              setPage(0);
            }}
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="text-xs font-mono uppercase tracking-[0.1em] text-muted-foreground">Vertical</label>
          <Select
            options={VERTICAL_OPTIONS_FILTER}
            value={vertical}
            onChange={(v) => {
              setVertical(v);
              setPage(0);
            }}
          />
        </div>
      </div>

      {showForm && (
        <form
          onSubmit={onCreate}
          className="space-y-3 border border-border bg-card p-4 rounded"
        >
          <div className="space-y-1">
            <label className="text-xs font-mono uppercase tracking-[0.1em] text-muted-foreground">Name</label>
            <Input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Auto 7-day nurture"
              required
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-mono uppercase tracking-[0.1em] text-muted-foreground">Vertical</label>
            <Select
              options={VERTICAL_OPTIONS}
              value={newVertical}
              onChange={setNewVertical}
            />
          </div>
          <div className="flex items-center gap-2">
            <Button type="submit" disabled={createMut.isPending}>
              {createMut.isPending ? "Creating..." : "Create"}
            </Button>
            {createMut.error && (
              <span className="text-sm font-mono text-danger">
                {(createMut.error as Error).message}
              </span>
            )}
          </div>
        </form>
      )}

      {isLoading && <div className="text-sm font-mono text-muted-foreground">Loading...</div>}
      {error && (
        <div className="text-sm font-mono text-danger">{(error as Error).message}</div>
      )}

      {!isLoading && !error && (
        <>
          <Table>
            <Thead>
              <Tr>
                <Th>Name</Th>
                <Th>Vertical</Th>
                <Th>Status</Th>
                <Th>Version</Th>
                <Th>Updated</Th>
                <Th></Th>
              </Tr>
            </Thead>
            <Tbody>
              {rows.length === 0 && (
                <Tr>
                  <Td colSpan={6} className="text-center font-mono text-xs text-muted-foreground">
                    No workflows
                  </Td>
                </Tr>
              )}
              {rows.map((wf) => (
                <Tr key={wf.id}>
                  <Td className="font-medium">{wf.name}</Td>
                  <Td className="font-mono text-xs">{wf.vertical}</Td>
                  <Td>
                    <Badge variant={statusVariant(wf.status)}>{wf.status}</Badge>
                  </Td>
                  <Td className="font-mono text-xs tabular-nums">v{wf.version}</Td>
                  <Td className="font-mono text-xs tabular-nums">{formatDate(wf.updated_at)}</Td>
                  <Td>
                    <Link
                      to={`/workflows/${wf.id}`}
                      className="text-xs font-mono text-primary hover:underline underline-offset-4"
                    >
                      Open
                    </Link>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>

          <div className="flex items-center justify-between text-xs font-mono text-muted-foreground">
            <span>
              {total} total &middot; page {page + 1}
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={!hasNext}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
