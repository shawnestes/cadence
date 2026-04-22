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
      <h2 className="text-2xl font-semibold">Recipients</h2>

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
                <Th>Tenant</Th>
                <Th>Vertical</Th>
                <Th>Status</Th>
                <Th>Last seen</Th>
                <Th></Th>
              </Tr>
            </Thead>
            <Tbody>
              {rows.length === 0 && (
                <Tr>
                  <Td colSpan={6} className="text-muted-foreground">
                    No recipients.
                  </Td>
                </Tr>
              )}
              {rows.map((r) => (
                <Tr key={r.id}>
                  <Td className="font-mono text-xs">{r.email}</Td>
                  <Td>{r.tenant}</Td>
                  <Td>{r.vertical}</Td>
                  <Td>{r.status}</Td>
                  <Td>{formatDate(r.last_seen_at)}</Td>
                  <Td>
                    <Link
                      to={`/recipients/${r.id}`}
                      className="text-sm font-medium underline-offset-4 hover:underline"
                    >
                      View
                    </Link>
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
