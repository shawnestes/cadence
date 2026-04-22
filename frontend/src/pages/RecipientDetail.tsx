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
      <div className="text-xs text-muted-foreground">{label}</div>
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

  if (isLoading) return <div>Loading...</div>;
  if (error)
    return (
      <div className="text-destructive">{(error as Error).message}</div>
    );
  if (!data) return <div className="text-muted-foreground">Not found.</div>;

  const { recipient, journeys, sends, events } = data;
  const consentSource =
    (recipient.attributes?.consent_source as string | undefined) ?? null;
  const consentAt =
    (recipient.attributes?.consent_at as string | undefined) ?? null;

  return (
    <div className="space-y-6">
      <div>
        <Link
          to="/recipients"
          className="text-sm text-muted-foreground hover:underline"
        >
          ← Recipients
        </Link>
        <h2 className="text-2xl font-semibold mt-1">{recipient.email}</h2>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Summary</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            <Field label="Tenant" value={recipient.tenant} />
            <Field label="Vertical" value={recipient.vertical} />
            <Field label="Status" value={recipient.status} />
            <Field label="External ID" value={recipient.external_id} />
            <Field label="Consent source" value={consentSource} />
            <Field label="Consent at" value={consentAt ? formatDate(consentAt) : null} />
            <Field label="First seen" value={formatDate(recipient.first_seen_at)} />
            <Field label="Last seen" value={formatDate(recipient.last_seen_at)} />
            <Field label="ID" value={<span className="font-mono text-xs">{recipient.id}</span>} />
          </div>
          <div className="mt-6">
            <div className="text-xs text-muted-foreground mb-2">Attributes</div>
            <pre className="text-xs bg-muted/40 rounded-md p-3 overflow-auto max-h-64">
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
                <Th>Current node</Th>
                <Th>Entered</Th>
                <Th>Completed</Th>
              </Tr>
            </Thead>
            <Tbody>
              {journeys.length === 0 && (
                <Tr>
                  <Td colSpan={5} className="text-muted-foreground">
                    No journeys.
                  </Td>
                </Tr>
              )}
              {journeys.slice(0, 20).map((j) => (
                <Tr key={j.id}>
                  <Td className="font-mono text-xs">{shortId(j.workflow_id)}</Td>
                  <Td>{j.status}</Td>
                  <Td className="font-mono text-xs">{j.current_node_id}</Td>
                  <Td>{formatDate(j.entered_at)}</Td>
                  <Td>{formatDate(j.completed_at)}</Td>
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
                  <Td colSpan={5} className="text-muted-foreground">
                    No sends.
                  </Td>
                </Tr>
              )}
              {sends.slice(0, 50).map((s) => (
                <Tr key={s.id}>
                  <Td className="font-mono text-xs">{shortId(s.campaign_id)}</Td>
                  <Td>{s.variant_label}</Td>
                  <Td>{s.status}</Td>
                  <Td>{formatDate(s.queued_at)}</Td>
                  <Td>{formatDate(s.sent_at)}</Td>
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
                  <Td colSpan={4} className="text-muted-foreground">
                    No events.
                  </Td>
                </Tr>
              )}
              {events.slice(0, 100).map((e) => (
                <Tr key={e.id}>
                  <Td>{e.event_type}</Td>
                  <Td>{e.event_subtype ?? "—"}</Td>
                  <Td className="max-w-xs truncate">
                    {e.url ? (
                      <a
                        href={e.url}
                        target="_blank"
                        rel="noreferrer"
                        className="underline-offset-4 hover:underline"
                      >
                        {e.url}
                      </a>
                    ) : (
                      "—"
                    )}
                  </Td>
                  <Td>{formatDate(e.occurred_at)}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
