import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createSuppression,
  deleteSuppression,
  listSuppressions,
  type ListSuppressionsParams,
} from "@/api/suppressions";
import type { SuppressionReason } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import {
  Table,
  Tbody,
  Td,
  Th,
  Thead,
  Tr,
} from "@/components/ui/Table";

const REASONS: SuppressionReason[] = [
  "unsubscribe",
  "hard_bounce",
  "complaint",
  "manual",
  "otto_push",
];

const PAGE_SIZE = 50;

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function Suppressions() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [page, setPage] = useState(0);

  const [newEmail, setNewEmail] = useState("");
  const [newReason, setNewReason] = useState<SuppressionReason>("manual");
  const [newSource, setNewSource] = useState("");

  const filter: ListSuppressionsParams = {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    ...(appliedSearch ? { email: appliedSearch } : {}),
  };

  const { data, isLoading, error } = useQuery({
    queryKey: ["suppressions", filter],
    queryFn: () => listSuppressions(filter),
  });

  const createMut = useMutation({
    mutationFn: createSuppression,
    onSuccess: () => {
      setNewEmail("");
      setNewSource("");
      qc.invalidateQueries({ queryKey: ["suppressions"] });
    },
  });

  const deleteMut = useMutation({
    mutationFn: (email: string) => deleteSuppression(email),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["suppressions"] }),
  });

  const total = data?.total ?? 0;
  const rows = data?.suppressions ?? [];
  const hasNext = (page + 1) * PAGE_SIZE < total;

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-semibold">Suppressions</h2>

      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setPage(0);
          setAppliedSearch(search.trim());
        }}
      >
        <Input
          placeholder="Search by exact email"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <Button type="submit">Search</Button>
        {appliedSearch && (
          <Button
            variant="outline"
            type="button"
            onClick={() => {
              setSearch("");
              setAppliedSearch("");
              setPage(0);
            }}
          >
            Clear
          </Button>
        )}
      </form>

      <form
        className="flex flex-wrap gap-2 items-end rounded-lg border border-border p-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!newEmail.trim()) return;
          createMut.mutate({
            email: newEmail.trim(),
            reason: newReason,
            source: newSource.trim() || "manual_ui",
            campaign_id: null,
            notes: null,
          });
        }}
      >
        <div className="flex-1 min-w-[200px]">
          <label className="text-xs text-muted-foreground">Email</label>
          <Input
            placeholder="user@example.com"
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Reason</label>
          <select
            value={newReason}
            onChange={(e) =>
              setNewReason(e.target.value as SuppressionReason)
            }
            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
          >
            {REASONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div className="flex-1 min-w-[160px]">
          <label className="text-xs text-muted-foreground">Source</label>
          <Input
            placeholder="manual_ui"
            value={newSource}
            onChange={(e) => setNewSource(e.target.value)}
          />
        </div>
        <Button type="submit" disabled={createMut.isPending}>
          {createMut.isPending ? "Adding..." : "Add"}
        </Button>
      </form>
      {createMut.error && (
        <div className="text-sm text-destructive">
          {(createMut.error as Error).message}
        </div>
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
                <Th>Email</Th>
                <Th>Reason</Th>
                <Th>Source</Th>
                <Th>Suppressed at</Th>
                <Th>Actions</Th>
              </Tr>
            </Thead>
            <Tbody>
              {rows.length === 0 && (
                <Tr>
                  <Td colSpan={5} className="text-muted-foreground">
                    No suppressions.
                  </Td>
                </Tr>
              )}
              {rows.map((s) => (
                <Tr key={s.id}>
                  <Td className="font-mono text-xs">{s.email}</Td>
                  <Td>{s.reason}</Td>
                  <Td>{s.source}</Td>
                  <Td>{formatDate(s.suppressed_at)}</Td>
                  <Td>
                    <Button
                      variant="destructive"
                      size="sm"
                      disabled={deleteMut.isPending}
                      onClick={() => {
                        if (confirm(`Delete suppression for ${s.email}?`)) {
                          deleteMut.mutate(s.email);
                        }
                      }}
                    >
                      Delete
                    </Button>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>

          <div className="flex items-center justify-between text-sm">
            <span className="text-muted-foreground">
              {total} total &middot; page {page + 1}
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                Prev
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
