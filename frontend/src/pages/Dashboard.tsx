import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Wifi, WifiOff } from "lucide-react";

import { getTimeseries, getTopline, streamTopline } from "@/api/stats";
import type {
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
            sub={`${fmt(topline.bounces)} bounces`}
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
            <div className="flex h-32 flex-col items-center justify-center gap-2 text-center">
              <span className="text-[0.65rem] font-mono uppercase tracking-[0.15em] text-muted-foreground">Coming Soon</span>
              <span className="text-xs text-muted-foreground max-w-xs">
                No backend endpoint for per-campaign volume yet.
              </span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>SES Reputation & Account Status</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex h-32 flex-col items-center justify-center gap-2 text-center">
              <span className="text-[0.65rem] font-mono uppercase tracking-[0.15em] text-muted-foreground">Not Wired</span>
              <span className="text-xs text-muted-foreground max-w-xs">
                Requires a backend endpoint that proxies SES account reputation data. No fake data shown.
              </span>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
