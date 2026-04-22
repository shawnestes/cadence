import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { searchRecipients, type SearchRecipientsArgs } from "@/api/recipients";
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

const PAGE_SIZE = 50;

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function Recipients() {
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [page, setPage] = useState(0);

  const args: SearchRecipientsArgs = {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    ...(appliedSearch ? { email: appliedSearch } : {}),
  };

  const { data, isLoading, error } = useQuery({
    queryKey: ["recipients", args],
    queryFn: () => searchRecipients(args),
  });

  const total = data?.total ?? 0;
  const rows = data?.recipients ?? [];
  const hasNext = (page + 1) * PAGE_SIZE < total;

  return (
    <div className="space-y-6">
      {/* Masthead */}
      <div className="border-b border-border pb-4">
        <h1 className="text-2xl font-semibold tracking-tight">Recipients</h1>
        <p className="mt-1 text-xs font-mono text-muted-foreground uppercase tracking-[0.1em]">
          Email address registry & journey history
        </p>
      </div>

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

      {isLoading && <div className="text-sm font-mono text-muted-foreground">Loading...</div>}
      {error && (
        <div className="text-sm font-mono text-danger">{(error as Error).message}</div>
      )}

      {!isLoading && !error && (
        <>
          <Table>
            <Thead>
              <Tr>
                <Th>Email</Th>
                <Th>Tenant</Th>
                <Th>Vertical</Th>
                <Th>Status</Th>
                <Th>Last Seen</Th>
                <Th></Th>
              </Tr>
            </Thead>
            <Tbody>
              {rows.length === 0 && (
                <Tr>
                  <Td colSpan={6} className="text-muted-foreground font-mono text-xs">
                    No recipients.
                  </Td>
                </Tr>
              )}
              {rows.map((r) => (
                <Tr key={r.id}>
                  <Td className="font-mono text-xs tabular-nums">{r.email}</Td>
                  <Td className="font-mono text-xs">{r.tenant}</Td>
                  <Td className="font-mono text-xs">{r.vertical}</Td>
                  <Td className="font-mono text-xs">{r.status}</Td>
                  <Td className="font-mono text-xs tabular-nums">{formatDate(r.last_seen_at)}</Td>
                  <Td>
                    <Link
                      to={`/recipients/${r.id}`}
                      className="text-xs font-mono text-primary hover:underline underline-offset-4"
                    >
                      View
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
