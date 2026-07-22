import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Wifi, WifiOff } from "lucide-react";

import { listAlerts } from "@/api/alerts";
import { getCampaignsVolume, getIspReputation, getSesReputation, getTimeseries, getTopline, streamTopline } from "@/api/stats";
import type {
  AlertOut,
  AlertsListOut,
  CampaignVolumeRow,
  CampaignsVolumeOut,
  IspReputationOut,
  IspReputationRow,
  SESReputationOut,
  StatsBucket,
  StatsWindow,
  TimeseriesOut,
  TimeseriesPoint,
  Topline,
  ToplineStreamEvent,
} from "@/api/types";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/Card";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function pct(rate: number): string {
  return (rate * 100).toFixed(2) + "%";
}

function fmt(n: number): string {
  return n.toLocaleString();
}

// "—" for null/undefined — CloudWatch-derived fields are null in dev and
// must never render as NaN/undefined.
function pctOrDash(rate: number | null | undefined): string {
  return rate == null ? "—" : pct(rate);
}

function fmtOrDash(n: number | null | undefined): string {
  return n == null ? "—" : fmt(n);
}

function bucketForWindow(window: StatsWindow): StatsBucket {
  return window === "24h" ? "1h" : "1d";
}

function formatBucketLabel(iso: string, bucket: StatsBucket): string {
  try {
    const d = new Date(iso);
    if (bucket === "1h") {
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// Window toggle
// ---------------------------------------------------------------------------

const WINDOWS: StatsWindow[] = ["24h", "7d", "30d"];

function WindowToggle({
  value,
  onChange,
}: {
  value: StatsWindow;
  onChange: (w: StatsWindow) => void;
}) {
  return (
    <div className="inline-flex border border-border p-0.5 gap-0.5">
      {WINDOWS.map((w) => (
        <button
          key={w}
          onClick={() => onChange(w)}
          className={[
            "px-3 py-1 text-xs font-mono uppercase tracking-[0.1em] transition-colors",
            value === w
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground",
          ].join(" ")}
        >
          {w}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stat card — chrome-free, big number signature
// ---------------------------------------------------------------------------

type StatCardProps = {
  title: string;
  value: string;
  sub?: string;
  alert?: boolean;
  warn?: boolean;
};

function StatCard({ title, value, sub, alert, warn }: StatCardProps) {
  const valueColor = alert
    ? "text-danger"
    : warn
      ? "text-warn"
      : "text-foreground";

  return (
    <div className="flex flex-col gap-1">
      <div className="text-[0.7rem] uppercase tracking-[0.15em] text-muted-foreground font-medium font-mono">
        {title}
        {alert && <AlertTriangle className="inline h-3 w-3 ml-1.5 text-danger" />}
      </div>
      <div className={`text-5xl font-bold font-mono tabular-nums leading-none ${valueColor}`}>
        {value}
      </div>
      {sub && (
        <div className="text-xs text-muted-foreground font-mono">{sub}</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Full-width alert banner for complaint rate violations
// ---------------------------------------------------------------------------

function ComplaintAlertBanner({ rate }: { rate: number }) {
  return (
    <div className="border border-[oklch(0.62_0.25_25_/_0.5)] bg-[oklch(0.62_0.25_25_/_0.1)] px-4 py-3 flex items-center gap-3">
      <AlertTriangle className="h-4 w-4 text-danger flex-shrink-0" />
      <div>
        <span className="text-sm font-mono font-medium text-danger uppercase tracking-[0.1em]">
          Complaint Rate Critical
        </span>
        <span className="text-sm text-danger ml-2">
          {pct(rate)} — exceeds 0.1% SES action threshold
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Active alerts banner
// ---------------------------------------------------------------------------

function ActiveAlertsBanner({ alerts }: { alerts: AlertOut[] }) {
  if (alerts.length === 0) return null;
  return (
    <div className="space-y-1">
      {alerts.map((a) => {
        const isCritical = a.severity === "critical";
        return (
          <div
            key={a.id}
            className={[
              "border px-4 py-3 flex items-start gap-3",
              isCritical
                ? "bg-danger/15 border-danger"
                : "bg-primary/10 border-primary",
            ].join(" ")}
          >
            <div className="flex-1 min-w-0">
              <span
                className={[
                  "text-sm font-mono font-medium uppercase tracking-[0.1em]",
                  isCritical ? "text-danger" : "text-primary",
                ].join(" ")}
              >
                {a.severity.toUpperCase()} — {a.metric.replace(/_/g, " ")}
              </span>
              <span
                className={[
                  "text-sm ml-2",
                  isCritical ? "text-danger" : "text-primary",
                ].join(" ")}
              >
                {(a.value * 100).toFixed(3)}% vs threshold {(a.threshold * 100).toFixed(3)}%
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recent alerts table
// ---------------------------------------------------------------------------

function RecentAlertsTable({ alerts }: { alerts: AlertOut[] }) {
  if (alerts.length === 0) {
    return (
      <div className="flex items-center justify-center py-6">
        <span className="text-[0.65rem] font-mono uppercase tracking-[0.15em] text-muted-foreground">
          No alerts in window
        </span>
      </div>
    );
  }

  function statusColor(status: string): string {
    if (status === "active") return "text-danger";
    if (status === "pending") return "text-warn";
    return "text-muted-foreground";
  }

  function severityColor(severity: string): string {
    return severity === "critical" ? "text-danger" : "text-primary";
  }

  function fmtTs(iso: string): string {
    try {
      return new Date(iso).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return iso;
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead>
          <tr className="border-b border-border">
            <th className="pb-2 text-left text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium">Time</th>
            <th className="pb-2 pr-3 text-left text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium">Metric</th>
            <th className="pb-2 pr-3 text-left text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium">Severity</th>
            <th className="pb-2 pr-3 text-left text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium">Status</th>
            <th className="pb-2 text-right text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium tabular-nums">Value / Threshold</th>
          </tr>
        </thead>
        <tbody>
          {alerts.map((a) => (
            <tr key={a.id} className="border-b border-border/40 last:border-0">
              <td className="py-1.5 pr-3 tabular-nums text-muted-foreground whitespace-nowrap">
                {fmtTs(a.created_at)}
              </td>
              <td className="py-1.5 pr-3 text-foreground">
                {a.metric.replace(/_/g, " ")}
              </td>
              <td className={`py-1.5 pr-3 uppercase ${severityColor(a.severity)}`}>
                {a.severity}
              </td>
              <td className={`py-1.5 pr-3 uppercase ${statusColor(a.status)}`}>
                {a.status}
              </td>
              <td className="py-1.5 text-right tabular-nums text-foreground">
                {(a.value * 100).toFixed(3)}% / {(a.threshold * 100).toFixed(3)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bar chart (inline divs)
// ---------------------------------------------------------------------------

type BarChartProps = {
  points: TimeseriesPoint[];
  bucket: StatsBucket;
  valueKey: keyof TimeseriesPoint;
  label: string;
  color?: string;
};

function BarChart({ points, bucket, valueKey, label, color = "bg-primary" }: BarChartProps) {
  if (points.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        No data for this window.
      </div>
    );
  }

  const values = points.map((p) => Number(p[valueKey]));
  const max = Math.max(...values, 1);

  return (
    <div>
      <p className="mb-2 text-[0.7rem] font-mono font-medium text-muted-foreground uppercase tracking-[0.15em]">
        {label}
      </p>
      <div className="flex items-end gap-[2px] h-32">
        {points.map((p, i) => {
          const h = Math.max((values[i] / max) * 100, values[i] > 0 ? 4 : 0);
          return (
            <div
              key={p.bucket_start}
              className="flex flex-1 flex-col items-center gap-1 group relative"
              title={`${formatBucketLabel(p.bucket_start, bucket)}: ${fmt(values[i])}`}
            >
              <div
                className={`w-full ${color} transition-all opacity-80`}
                style={{ height: `${h}%` }}
              />
              <span className="pointer-events-none absolute bottom-full mb-1 hidden group-hover:block whitespace-nowrap rounded border border-border bg-popover px-1.5 py-0.5 text-xs font-mono text-popover-foreground">
                {formatBucketLabel(p.bucket_start, bucket)}: {fmt(values[i])}
              </span>
            </div>
          );
        })}
      </div>
      {points.length > 0 && (
        <div className="mt-1 flex justify-between text-[10px] font-mono text-muted-foreground">
          <span>{formatBucketLabel(points[0].bucket_start, bucket)}</span>
          {points.length > 2 && (
            <span>
              {formatBucketLabel(
                points[Math.floor(points.length / 2)].bucket_start,
                bucket,
              )}
            </span>
          )}
          <span>
            {formatBucketLabel(points[points.length - 1].bucket_start, bucket)}
          </span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rate-over-time chart with reference line
// ---------------------------------------------------------------------------

type RateLineChartProps = {
  points: TimeseriesPoint[];
  bucket: StatsBucket;
  numKey: keyof TimeseriesPoint;
  denKey: keyof TimeseriesPoint;
  label: string;
  threshold?: number;
  thresholdLabel?: string;
  fillColor?: string;
};

function RateLineChart({
  points,
  bucket,
  numKey,
  denKey,
  label,
  threshold,
  thresholdLabel,
  fillColor = "oklch(0.82 0.18 85)",
}: RateLineChartProps) {
  const CHART_H = 128;

  if (points.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        No data for this window.
      </div>
    );
  }

  const rates = points.map((p) => {
    const n = Number(p[numKey]);
    const d = Number(p[denKey]);
    return d > 0 ? n / d : 0;
  });

  const maxRate = Math.max(...rates, threshold ? threshold * 2 : 0.01);
  const toY = (r: number) => CHART_H - (r / maxRate) * CHART_H;

  const W = 400;
  const barW = W / Math.max(points.length, 1);

  return (
    <div>
      <p className="mb-2 text-[0.7rem] font-mono font-medium text-muted-foreground uppercase tracking-[0.15em]">
        {label}
      </p>
      <div className="relative w-full overflow-hidden" style={{ height: CHART_H }}>
        <svg
          viewBox={`0 0 ${W} ${CHART_H}`}
          preserveAspectRatio="none"
          className="w-full h-full"
        >
          {threshold !== undefined && (
            <line
              x1={0}
              x2={W}
              y1={toY(threshold)}
              y2={toY(threshold)}
              strokeDasharray="4 3"
              strokeWidth={1.5}
              stroke="oklch(0.62 0.25 25)"
            />
          )}
          {points.map((_, i) => {
            const y = toY(rates[i]);
            const barH = CHART_H - y;
            return (
              <rect
                key={i}
                x={i * barW + 1}
                y={y}
                width={Math.max(barW - 2, 1)}
                height={barH > 0 ? barH : 0}
                fill={fillColor}
                opacity={0.8}
              />
            );
          })}
        </svg>
      </div>
      {points.length > 0 && (
        <div className="mt-1 flex justify-between text-[10px] font-mono text-muted-foreground">
          <span>{formatBucketLabel(points[0].bucket_start, bucket)}</span>
          {points.length > 2 && (
            <span>
              {formatBucketLabel(
                points[Math.floor(points.length / 2)].bucket_start,
                bucket,
              )}
            </span>
          )}
          <span>
            {formatBucketLabel(points[points.length - 1].bucket_start, bucket)}
          </span>
        </div>
      )}
      {threshold !== undefined && thresholdLabel && (
        <p className="mt-1 text-[10px] font-mono text-danger">
          — {thresholdLabel} ({pct(threshold)} SES threshold)
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-ISP reputation
// ---------------------------------------------------------------------------

const ISP_ORDER: IspReputationRow["isp"][] = ["Gmail", "Yahoo", "Outlook", "Apple", "Other"];

function ispSort(a: IspReputationRow, b: IspReputationRow): number {
  return ISP_ORDER.indexOf(a.isp) - ISP_ORDER.indexOf(b.isp);
}

// Buckets a rate into an alert tier. `null` (no CloudWatch data yet) is its
// own neutral tier — never conflated with a real bad rate.
type RateTier = "none" | "danger" | "warn" | "good";

function rateTier(rate: number | null): RateTier {
  if (rate == null) return "none";
  if (rate < 0.9) return "danger";
  if (rate < 0.97) return "warn";
  return "good";
}

const TIER_BG: Record<RateTier, string> = {
  none: "bg-border",
  danger: "bg-danger",
  warn: "bg-warn",
  good: "bg-primary",
};

const TIER_TEXT: Record<RateTier, string> = {
  none: "text-muted-foreground",
  danger: "text-danger",
  warn: "text-warn",
  good: "text-foreground",
};

function PoolHealthStrip({
  sendingAllowed,
  dedicatedIpCount,
}: {
  sendingAllowed: boolean | null;
  dedicatedIpCount: number | null;
}) {
  const allowedLabel =
    sendingAllowed === null ? "Unknown" : sendingAllowed ? "Allowed" : "Paused";
  const allowedColor =
    sendingAllowed === null
      ? "text-muted-foreground"
      : sendingAllowed
        ? "text-success"
        : "text-danger";
  const dotColor =
    sendingAllowed === null
      ? "bg-muted-foreground"
      : sendingAllowed
        ? "bg-success"
        : "bg-danger";

  return (
    <div className="flex flex-wrap items-center gap-x-8 gap-y-2 border border-border bg-background/40 px-4 py-3 font-mono text-xs">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full flex-shrink-0 ${dotColor}`} />
        <span className="text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">
          Sending Pool
        </span>
        <span className={`font-medium ${allowedColor}`}>{allowedLabel}</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">
          Dedicated IPs
        </span>
        <span className="tabular-nums text-foreground">{fmtOrDash(dedicatedIpCount)}</span>
      </div>
    </div>
  );
}

function IspTile({ row }: { row: IspReputationRow }) {
  const tier = rateTier(row.delivery_rate);
  const repFraction =
    row.reputation == null ? null : Math.max(0, Math.min(1, row.reputation));

  return (
    <div className="border border-border p-3 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-mono font-semibold uppercase tracking-[0.1em] text-foreground">
          {row.isp}
        </span>
        <span className={`text-xs font-mono tabular-nums ${TIER_TEXT[tier]}`}>
          {pctOrDash(row.delivery_rate)}
        </span>
      </div>

      {/* Reputation gauge */}
      <div>
        <div className="flex justify-between mb-1">
          <span className="text-[0.6rem] uppercase tracking-[0.15em] text-muted-foreground font-mono">
            Reputation
          </span>
          <span className="text-[0.65rem] font-mono tabular-nums text-muted-foreground">
            {pctOrDash(row.reputation)}
          </span>
        </div>
        <div className="h-1.5 w-full bg-border overflow-hidden">
          {repFraction !== null && (
            <div
              className={`h-full transition-all ${TIER_BG[rateTier(row.reputation)]}`}
              style={{ width: `${(repFraction * 100).toFixed(1)}%` }}
            />
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 text-[0.65rem] font-mono">
        <div>
          <div className="uppercase tracking-[0.15em] text-muted-foreground">Sent</div>
          <div className="tabular-nums text-foreground">{fmt(row.sent)}</div>
        </div>
        <div>
          <div className="uppercase tracking-[0.15em] text-muted-foreground">Delivered</div>
          <div className="tabular-nums text-foreground">{fmt(row.delivered)}</div>
        </div>
        <div className="col-span-2">
          <div className="uppercase tracking-[0.15em] text-muted-foreground">
            Effective Daily Rate
          </div>
          <div className="tabular-nums text-foreground">
            {row.effective_daily_rate == null
              ? "— no data"
              : `${fmt(Math.round(row.effective_daily_rate))}/day`}
          </div>
        </div>
      </div>
    </div>
  );
}

function IspDeliveryBarChart({ isps }: { isps: IspReputationRow[] }) {
  if (isps.length === 0) {
    return (
      <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">
        No ISP data for this window.
      </div>
    );
  }

  return (
    <div>
      <p className="mb-3 text-[0.7rem] font-mono font-medium text-muted-foreground uppercase tracking-[0.15em]">
        Delivery Rate by ISP
      </p>
      <div className="space-y-2.5">
        {isps.map((row) => {
          const tier = rateTier(row.delivery_rate);
          const widthPct =
            row.delivery_rate == null
              ? 0
              : Math.max(0, Math.min(1, row.delivery_rate)) * 100;
          return (
            <div key={row.isp} className="flex items-center gap-3">
              <span className="w-16 flex-shrink-0 text-[0.65rem] font-mono uppercase tracking-[0.1em] text-muted-foreground">
                {row.isp}
              </span>
              <div className="h-2.5 flex-1 bg-border overflow-hidden">
                {row.delivery_rate != null && (
                  <div
                    className={`h-full transition-all ${TIER_BG[tier]}`}
                    style={{ width: `${widthPct.toFixed(1)}%` }}
                  />
                )}
              </div>
              <span className="w-14 flex-shrink-0 text-right text-[0.65rem] font-mono tabular-nums text-muted-foreground">
                {pctOrDash(row.delivery_rate)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SSE connection indicator
// ---------------------------------------------------------------------------

type SseStatus = "connecting" | "live" | "error";

function SseIndicator({ status }: { status: SseStatus }) {
  if (status === "live") {
    return (
      <span className="flex items-center gap-1 text-xs font-mono text-success">
        <Wifi className="h-3 w-3" />
        Live
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="flex items-center gap-1 text-xs font-mono text-warn">
        <WifiOff className="h-3 w-3" />
        Reconnecting
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1 text-xs font-mono text-muted-foreground">
      <Wifi className="h-3 w-3 animate-pulse" />
      Connecting
    </span>
  );
}

// ---------------------------------------------------------------------------
// Main Dashboard component
// ---------------------------------------------------------------------------

const TENANT = "otto";

export function Dashboard() {
  const [statsWindow, setStatsWindow] = useState<StatsWindow>("24h");
  const [liveTopline, setLiveTopline] = useState<Topline | null>(null);
  const [sseStatus, setSseStatus] = useState<SseStatus>("connecting");
  const esRef = useRef<EventSource | null>(null);

  const {
    data: toplineData,
    isLoading: toplineLoading,
    error: toplineError,
  } = useQuery({
    queryKey: ["topline", statsWindow, TENANT],
    queryFn: () => getTopline(statsWindow, TENANT),
  });

  const bucket = bucketForWindow(statsWindow);
  const {
    data: timeseriesData,
    isLoading: tsLoading,
    error: tsError,
  } = useQuery<TimeseriesOut>({
    queryKey: ["timeseries", statsWindow, bucket, TENANT],
    queryFn: () => getTimeseries(statsWindow, bucket, TENANT),
  });

  const {
    data: campaignsData,
    isLoading: campaignsLoading,
    error: campaignsError,
  } = useQuery<CampaignsVolumeOut>({
    queryKey: ["campaigns-volume", statsWindow, TENANT],
    queryFn: () => getCampaignsVolume(statsWindow, TENANT, 10),
  });

  const {
    data: sesData,
    isLoading: sesLoading,
    error: sesError,
  } = useQuery<SESReputationOut>({
    queryKey: ["ses-reputation", TENANT],
    queryFn: () => getSesReputation(TENANT),
    retry: false,
  });

  const {
    data: ispData,
    isLoading: ispLoading,
    error: ispError,
  } = useQuery<IspReputationOut>({
    queryKey: ["isp-reputation", TENANT],
    queryFn: () => getIspReputation(TENANT),
    retry: false,
  });

  const { data: activeAlertsData } = useQuery<AlertsListOut>({
    queryKey: ["alerts-active", TENANT],
    queryFn: () => listAlerts({ tenant: TENANT, status: "active", limit: 20 }),
    refetchInterval: 60_000,
  });

  const { data: recentAlertsData } = useQuery<AlertsListOut>({
    queryKey: ["alerts-recent", TENANT],
    queryFn: () => listAlerts({ tenant: TENANT, status: "all", limit: 10, offset: 0 }),
    refetchInterval: 60_000,
  });

  const activeAlerts: AlertOut[] = activeAlertsData?.alerts ?? [];
  const recentAlerts: AlertOut[] = recentAlertsData?.alerts ?? [];

  useEffect(() => {
    esRef.current?.close();
    setSseStatus("connecting");
    setLiveTopline(null);

    const es = streamTopline(TENANT, statsWindow);
    esRef.current = es;

    es.onopen = () => setSseStatus("live");

    es.onmessage = (evt) => {
      try {
        const payload = JSON.parse(evt.data) as ToplineStreamEvent;
        if (payload.type === "topline") {
          setSseStatus("live");
          setLiveTopline(payload.data);
        }
      } catch {
        // ignore parse errors
      }
    };

    es.onerror = () => {
      setSseStatus("error");
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [statsWindow]);

  const topline: Topline | null =
    liveTopline ?? toplineData?.topline ?? null;

  const points: TimeseriesPoint[] = timeseriesData?.points ?? [];

  return (
    <div className="space-y-6">
      {/* Page masthead */}
      <div className="border-b border-border pb-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Deliverability Dashboard</h1>
            <p className="mt-1 text-xs font-mono text-muted-foreground uppercase tracking-[0.1em]">
              Tenant: {TENANT}
            </p>
          </div>
          <div className="flex items-center gap-4">
            <SseIndicator status={sseStatus} />
            <WindowToggle value={statsWindow} onChange={setStatsWindow} />
          </div>
        </div>
      </div>

      {/* Active alerts banner */}
      <ActiveAlertsBanner alerts={activeAlerts} />

      {/* Complaint rate full-width alert banner */}
      {topline && topline.complaint_rate >= 0.001 && (
        <ComplaintAlertBanner rate={topline.complaint_rate} />
      )}

      {/* Loading / error states */}
      {toplineLoading && !topline && (
        <p className="text-sm font-mono text-muted-foreground">Loading...</p>
      )}
      {toplineError && (
        <div className="border border-[oklch(0.62_0.25_25_/_0.4)] bg-[oklch(0.62_0.25_25_/_0.1)] px-4 py-3 text-sm font-mono text-danger">
          Failed to load metrics: {(toplineError as Error).message}
        </div>
      )}

      {/* ---- Topline stat cards — chrome-free grid ---- */}
      {topline && (
        <div className="grid gap-x-8 gap-y-8 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            title="Sends"
            value={fmt(topline.sends)}
            sub={`${fmt(topline.delivered)} delivered`}
          />
          <StatCard
            title="Delivery Rate"
            value={pct(topline.delivery_rate)}
            sub={`${fmt(topline.delivered)} / ${fmt(topline.sends)}`}
          />
          <StatCard
            title="Open Rate"
            value={pct(topline.open_rate)}
            sub={`${fmt(topline.unique_opens)} unique opens`}
          />
          <StatCard
            title="Click Rate"
            value={pct(topline.click_rate)}
            sub={`${fmt(topline.unique_clicks)} unique clicks`}
          />
          <StatCard
            title="Bounce Rate"
            value={pct(topline.bounce_rate)}
            sub={`${fmt(topline.hard_bounces)} hard, ${fmt(topline.soft_bounces)} soft`}
            warn={topline.bounce_rate > 0.03}
            alert={topline.bounce_rate >= 0.05}
          />
          <StatCard
            title="Complaint Rate"
            value={pct(topline.complaint_rate)}
            sub={`${fmt(topline.complaints)} complaints`}
            warn={topline.complaint_rate > 0.0008}
            alert={topline.complaint_rate >= 0.001}
          />
          <StatCard
            title="Unsubscribe Rate"
            value={pct(topline.unsubscribe_rate)}
            sub={`${fmt(topline.unsubscribes)} unsubscribes`}
          />
        </div>
      )}

      {/* ---- Charts ---- */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Sends Over Time</CardTitle>
          </CardHeader>
          <CardContent>
            {tsLoading ? (
              <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
                Loading...
              </div>
            ) : tsError ? (
              <div className="text-sm text-danger">
                {(tsError as Error).message}
              </div>
            ) : (
              <BarChart
                points={points}
                bucket={bucket}
                valueKey="sends"
                label={`Sends per ${bucket === "1h" ? "hour" : "day"}`}
                color="bg-primary"
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Bounce & Complaint Rates</CardTitle>
          </CardHeader>
          <CardContent className="space-y-6">
            {tsLoading ? (
              <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
                Loading...
              </div>
            ) : tsError ? (
              <div className="text-sm text-danger">
                {(tsError as Error).message}
              </div>
            ) : (
              <>
                <RateLineChart
                  points={points}
                  bucket={bucket}
                  numKey="bounces"
                  denKey="sends"
                  label="Bounce Rate"
                  threshold={0.05}
                  thresholdLabel="SES bounce threshold"
                  fillColor="oklch(0.78 0.18 75)"
                />
                <RateLineChart
                  points={points}
                  bucket={bucket}
                  numKey="complaints"
                  denKey="sends"
                  label="Complaint Rate"
                  threshold={0.001}
                  thresholdLabel="SES complaint threshold"
                  fillColor="oklch(0.62 0.25 25)"
                />
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Top 10 Campaigns by Volume</CardTitle>
          </CardHeader>
          <CardContent>
            {campaignsLoading ? (
              <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
                Loading...
              </div>
            ) : campaignsError ? (
              <div className="text-sm text-danger">
                {(campaignsError as Error).message}
              </div>
            ) : !campaignsData || campaignsData.campaigns.length === 0 ? (
              <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
                No campaign data for this window.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="pb-2 text-left text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium">Campaign</th>
                      <th className="pb-2 pr-3 text-right text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium tabular-nums">Sends</th>
                      <th className="pb-2 pr-3 text-right text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium tabular-nums">Delivery</th>
                      <th className="pb-2 text-right text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground font-medium tabular-nums">Bounce</th>
                    </tr>
                  </thead>
                  <tbody>
                    {campaignsData.campaigns.map((row: CampaignVolumeRow) => (
                      <tr key={row.campaign_id} className="border-b border-border/40 last:border-0">
                        <td className="py-1.5 pr-3 truncate max-w-[12rem] text-foreground" title={row.campaign_name}>
                          {row.campaign_name}
                        </td>
                        <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                          {fmt(row.sends)}
                        </td>
                        <td className={`py-1.5 pr-3 text-right tabular-nums ${row.delivery_rate < 0.9 ? "text-warn" : "text-foreground"}`}>
                          {pct(row.delivery_rate)}
                        </td>
                        <td className={`py-1.5 text-right tabular-nums ${row.bounce_rate >= 0.05 ? "text-danger" : row.bounce_rate > 0.03 ? "text-warn" : "text-foreground"}`}>
                          {pct(row.bounce_rate)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>SES Reputation & Account Status</CardTitle>
          </CardHeader>
          <CardContent>
            {sesLoading ? (
              <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
                Loading...
              </div>
            ) : sesError ? (
              <div className="flex h-32 flex-col items-center justify-center gap-1 text-center">
                <span className="text-[0.65rem] font-mono uppercase tracking-[0.15em] text-muted-foreground">Not Available</span>
                <span className="text-xs text-muted-foreground">{(sesError as Error).message}</span>
              </div>
            ) : sesData ? (
              <div className="space-y-4 font-mono text-xs">
                <div className="grid grid-cols-2 gap-x-6 gap-y-2">
                  <div>
                    <div className="text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">Sending</div>
                    <div className={sesData.sending_enabled ? "text-success" : "text-danger"}>
                      {sesData.sending_enabled ? "Enabled" : "Disabled"}
                    </div>
                  </div>
                  <div>
                    <div className="text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">Production</div>
                    <div className={sesData.production_access_enabled ? "text-success" : "text-warn"}>
                      {sesData.production_access_enabled ? "Enabled" : "Sandbox"}
                    </div>
                  </div>
                  {sesData.enforcement_status && (
                    <div className="col-span-2">
                      <div className="text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">Enforcement</div>
                      <div className="text-danger">{sesData.enforcement_status}</div>
                    </div>
                  )}
                  <div>
                    <div className="text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">Max Rate</div>
                    <div className="tabular-nums text-foreground">{sesData.max_send_rate.toFixed(1)}/s</div>
                  </div>
                  <div>
                    <div className="text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">24h Quota</div>
                    <div className="tabular-nums text-foreground">{fmt(Math.round(sesData.max_24_hour_send))}</div>
                  </div>
                </div>
                <div>
                  <div className="flex justify-between mb-1">
                    <span className="text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">Sent Last 24h</span>
                    <span className="tabular-nums text-muted-foreground">
                      {fmt(Math.round(sesData.sent_last_24_hours))} / {fmt(Math.round(sesData.max_24_hour_send))}
                    </span>
                  </div>
                  <div className="h-2 w-full bg-border overflow-hidden">
                    <div
                      className={`h-full transition-all ${
                        sesData.max_24_hour_send > 0 && sesData.sent_last_24_hours / sesData.max_24_hour_send >= 0.9
                          ? "bg-danger"
                          : sesData.max_24_hour_send > 0 && sesData.sent_last_24_hours / sesData.max_24_hour_send >= 0.7
                            ? "bg-warn"
                            : "bg-primary"
                      }`}
                      style={{
                        width: sesData.max_24_hour_send > 0
                          ? `${Math.min((sesData.sent_last_24_hours / sesData.max_24_hour_send) * 100, 100).toFixed(1)}%`
                          : "0%",
                      }}
                    />
                  </div>
                </div>
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>

      {/* ---- Per-ISP reputation ---- */}
      <div>
        <p className="mb-3 text-[0.7rem] font-mono font-medium text-muted-foreground uppercase tracking-[0.15em]">
          Per-ISP Reputation
        </p>
        <Card>
          <CardHeader>
            <CardTitle>Inbox Provider Health</CardTitle>
          </CardHeader>
          <CardContent className="space-y-6">
            {ispLoading ? (
              <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
                Loading...
              </div>
            ) : ispError ? (
              <div className="flex h-32 flex-col items-center justify-center gap-1 text-center">
                <span className="text-[0.65rem] font-mono uppercase tracking-[0.15em] text-muted-foreground">
                  Not Available
                </span>
                <span className="text-xs text-muted-foreground">
                  {(ispError as Error).message}
                </span>
              </div>
            ) : ispData ? (
              <>
                <PoolHealthStrip
                  sendingAllowed={ispData.sending_allowed}
                  dedicatedIpCount={ispData.dedicated_ip_count}
                />

                {ispData.isps.length === 0 ? (
                  <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">
                    No ISP data for this window.
                  </div>
                ) : (
                  <>
                    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                      {[...ispData.isps].sort(ispSort).map((row) => (
                        <IspTile key={row.isp} row={row} />
                      ))}
                    </div>

                    <IspDeliveryBarChart isps={[...ispData.isps].sort(ispSort)} />
                  </>
                )}
              </>
            ) : null}
          </CardContent>
        </Card>
      </div>

      {/* ---- Recent alerts ---- */}
      <div>
        <p className="mb-3 text-[0.7rem] font-mono font-medium text-muted-foreground uppercase tracking-[0.15em]">
          Recent Alerts
        </p>
        <RecentAlertsTable alerts={recentAlerts} />
      </div>
    </div>
  );
}
