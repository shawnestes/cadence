import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getRecipient } from "@/api/recipients";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/Card";
import {
  Table,
  Tbody,
  Td,
  Th,
  Thead,
  Tr,
} from "@/components/ui/Table";

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return "—";
  return id.slice(0, n);
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-[0.65rem] font-mono uppercase tracking-[0.12em] text-muted-foreground mb-0.5">{label}</div>
      <div className="text-sm font-medium break-all">{value ?? "—"}</div>
    </div>
  );
}

export function RecipientDetail() {
  const { id = "" } = useParams<{ id: string }>();

  const { data, isLoading, error } = useQuery({
    queryKey: ["recipient", id],
    queryFn: () => getRecipient(id),
    enabled: Boolean(id),
  });

  if (isLoading) return <div className="text-sm font-mono text-muted-foreground">Loading...</div>;
  if (error)
    return (
      <div className="text-sm font-mono text-danger">{(error as Error).message}</div>
    );
  if (!data) return <div className="text-muted-foreground">Not found.</div>;

  const { recipient, journeys, sends, events } = data;
  const consentSource =
    (recipient.attributes?.consent_source as string | undefined) ?? null;
  const consentAt =
    (recipient.attributes?.consent_at as string | undefined) ?? null;

  return (
    <div className="space-y-6">
      {/* Masthead */}
      <div className="border-b border-border pb-4">
        <Link
          to="/recipients"
          className="text-xs font-mono text-muted-foreground hover:text-foreground uppercase tracking-[0.1em]"
        >
          &larr; Recipients
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight mt-2 font-mono">{recipient.email}</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Summary</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            <Field label="Tenant" value={<span className="font-mono">{recipient.tenant}</span>} />
            <Field label="Vertical" value={<span className="font-mono">{recipient.vertical}</span>} />
            <Field label="Status" value={<span className="font-mono">{recipient.status}</span>} />
            <Field label="External ID" value={<span className="font-mono text-xs">{recipient.external_id}</span>} />
            <Field label="Consent Source" value={consentSource ? <span className="font-mono text-xs">{consentSource}</span> : null} />
            <Field label="Consent At" value={consentAt ? <span className="font-mono text-xs tabular-nums">{formatDate(consentAt)}</span> : null} />
            <Field label="First Seen" value={<span className="font-mono text-xs tabular-nums">{formatDate(recipient.first_seen_at)}</span>} />
            <Field label="Last Seen" value={<span className="font-mono text-xs tabular-nums">{formatDate(recipient.last_seen_at)}</span>} />
            <Field label="ID" value={<span className="font-mono text-xs tabular-nums">{recipient.id}</span>} />
          </div>
          <div className="mt-6">
            <div className="text-[0.65rem] font-mono uppercase tracking-[0.12em] text-muted-foreground mb-2">Attributes</div>
            <pre className="text-xs font-mono bg-surface-2 rounded border border-border p-3 overflow-auto max-h-64">
              {JSON.stringify(recipient.attributes ?? {}, null, 2)}
            </pre>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Journeys</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <Thead>
              <Tr>
                <Th>Workflow</Th>
                <Th>Status</Th>
                <Th>Current Node</Th>
                <Th>Entered</Th>
                <Th>Completed</Th>
              </Tr>
            </Thead>
            <Tbody>
              {journeys.length === 0 && (
                <Tr>
                  <Td colSpan={5} className="font-mono text-xs text-muted-foreground">
                    No journeys.
                  </Td>
                </Tr>
              )}
              {journeys.slice(0, 20).map((j) => (
                <Tr key={j.id}>
                  <Td className="font-mono text-xs tabular-nums">{shortId(j.workflow_id)}</Td>
                  <Td className="font-mono text-xs">{j.status}</Td>
                  <Td className="font-mono text-xs">{j.current_node_id}</Td>
                  <Td className="font-mono text-xs tabular-nums">{formatDate(j.entered_at)}</Td>
                  <Td className="font-mono text-xs tabular-nums">{formatDate(j.completed_at)}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Sends</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <Thead>
              <Tr>
                <Th>Campaign</Th>
                <Th>Variant</Th>
                <Th>Status</Th>
                <Th>Queued</Th>
                <Th>Sent</Th>
              </Tr>
            </Thead>
            <Tbody>
              {sends.length === 0 && (
                <Tr>
                  <Td colSpan={5} className="font-mono text-xs text-muted-foreground">
                    No sends.
                  </Td>
                </Tr>
              )}
              {sends.slice(0, 50).map((s) => (
                <Tr key={s.id}>
                  <Td className="font-mono text-xs tabular-nums">{shortId(s.campaign_id)}</Td>
                  <Td className="font-mono text-xs">{s.variant_label}</Td>
                  <Td className="font-mono text-xs">{s.status}</Td>
                  <Td className="font-mono text-xs tabular-nums">{formatDate(s.queued_at)}</Td>
                  <Td className="font-mono text-xs tabular-nums">{formatDate(s.sent_at)}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Events</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <Thead>
              <Tr>
                <Th>Type</Th>
                <Th>Subtype</Th>
                <Th>URL</Th>
                <Th>Occurred</Th>
              </Tr>
            </Thead>
            <Tbody>
              {events.length === 0 && (
                <Tr>
                  <Td colSpan={4} className="font-mono text-xs text-muted-foreground">
                    No events.
                  </Td>
                </Tr>
              )}
              {events.slice(0, 100).map((e) => (
                <Tr key={e.id}>
                  <Td className="font-mono text-xs">{e.event_type}</Td>
                  <Td className="font-mono text-xs">{e.event_subtype ?? "—"}</Td>
                  <Td className="max-w-xs truncate font-mono text-xs">
                    {e.url ? (
                      <a
                        href={e.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-primary underline-offset-4 hover:underline"
                      >
                        {e.url}
                      </a>
                    ) : (
                      "—"
                    )}
                  </Td>
                  <Td className="font-mono text-xs tabular-nums">{formatDate(e.occurred_at)}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
