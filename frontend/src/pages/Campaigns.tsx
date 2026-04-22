import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createCampaign,
  listCampaigns,
  type ListCampaignsArgs,
} from "@/api/campaigns";
import type { CampaignIn } from "@/api/types";
import { Badge, statusVariant } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Table, Tbody, Td, Th, Thead, Tr } from "@/components/ui/Table";
import { Textarea } from "@/components/ui/Textarea";

const PAGE_SIZE = 25;

const STATUS_OPTIONS = [
  { label: "All", value: "" },
  { label: "draft", value: "draft" },
  { label: "generating", value: "generating" },
  { label: "ready", value: "ready" },
  { label: "active", value: "active" },
  { label: "paused", value: "paused" },
  { label: "archived", value: "archived" },
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

export function Campaigns() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(0);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [vertical, setVertical] = useState("auto");
  const [aiBrief, setAiBrief] = useState("");

  const args: ListCampaignsArgs = {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    ...(status ? { status } : {}),
  };

  const { data, isLoading, error } = useQuery({
    queryKey: ["campaigns", args],
    queryFn: () => listCampaigns(args),
  });

  const createMut = useMutation({
    mutationFn: (payload: CampaignIn) => createCampaign(payload),
    onSuccess: () => {
      setName("");
      setAiBrief("");
      setVertical("auto");
      setShowForm(false);
      qc.invalidateQueries({ queryKey: ["campaigns"] });
    },
  });

  const total = data?.total ?? 0;
  const rows = data?.campaigns ?? [];
  const hasNext = (page + 1) * PAGE_SIZE < total;

  const onCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    createMut.mutate({
      name: name.trim(),
      vertical,
      ai_brief: aiBrief.trim() || null,
    });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-semibold">Campaigns</h2>
        <Button onClick={() => setShowForm((s) => !s)}>
          {showForm ? "Cancel" : "New Campaign"}
        </Button>
      </div>

      <div className="flex items-center gap-2">
        <label className="text-sm text-muted-foreground">Status</label>
        <Select
          options={STATUS_OPTIONS}
          value={status}
          onChange={(v) => {
            setStatus(v);
            setPage(0);
          }}
        />
      </div>

      {showForm && (
        <form
          onSubmit={onCreate}
          className="space-y-3 rounded-lg border border-border bg-card p-4"
        >
          <div className="space-y-1">
            <label className="text-sm font-medium">Name</label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Spring auto re-shop"
              required
            />
          </div>
          <div className="space-y-1">
            <label className="text-sm font-medium">Vertical</label>
            <Select
              options={VERTICAL_OPTIONS}
              value={vertical}
              onChange={setVertical}
            />
          </div>
          <div className="space-y-1">
            <label className="text-sm font-medium">AI brief</label>
            <Textarea
              value={aiBrief}
              onChange={(e) => setAiBrief(e.target.value)}
              placeholder="Describe tone, offer, audience, ..."
            />
          </div>
          <div className="flex items-center gap-2">
            <Button type="submit" disabled={createMut.isPending}>
              {createMut.isPending ? "Creating..." : "Create"}
            </Button>
            {createMut.error && (
              <span className="text-sm text-destructive">
                {(createMut.error as Error).message}
              </span>
            )}
          </div>
        </form>
      )}

      {isLoading && <div>Loading...</div>}
      {error && (
        <div className="text-destructive">{(error as Error).message}</div>
      )}

      {!isLoading && !error && (
        <>
          <Table>
            <Thead>
              <Tr>
                <Th>Name</Th>
                <Th>Vertical</Th>
                <Th>Status</Th>
                <Th>Created</Th>
                <Th></Th>
              </Tr>
            </Thead>
            <Tbody>
              {rows.length === 0 && (
                <Tr>
                  <Td colSpan={5} className="text-center text-muted-foreground">
                    No campaigns
                  </Td>
                </Tr>
              )}
              {rows.map((c) => (
                <Tr key={c.id}>
                  <Td className="font-medium">{c.name}</Td>
                  <Td>{c.vertical}</Td>
                  <Td>
                    <Badge variant={statusVariant(c.status)}>{c.status}</Badge>
                  </Td>
                  <Td>{formatDate(c.created_at)}</Td>
                  <Td>
                    <Link
                      to={`/campaigns/${c.id}`}
                      className="text-primary hover:underline"
                    >
                      Open
                    </Link>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>

          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>
              {total} total · page {page + 1}
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
