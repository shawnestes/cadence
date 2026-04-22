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
import { Badge } from "@/components/ui/Badge";

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
    <div className="inline-flex rounded-md border border-border bg-muted p-0.5 gap-0.5">
      {WINDOWS.map((w) => (
        <button
          key={w}
          onClick={() => onChange(w)}
          className={[
            "rounded px-3 py-1 text-sm font-medium transition-colors",
            value === w
              ? "bg-background text-foreground shadow-sm"
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
// Stat card
// ---------------------------------------------------------------------------

type StatCardProps = {
  title: string;
  value: string;
  sub?: string;
  alert?: boolean;
  warn?: boolean;
};

function StatCard({ title, value, sub, alert, warn }: StatCardProps) {
  const borderCls = alert
    ? "border-destructive"
    : warn
      ? "border-yellow-400"
      : "";
  return (
    <Card className={borderCls}>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-end gap-2">
          <span
            className={[
              "text-2xl font-bold tabular-nums",
              alert ? "text-destructive" : warn ? "text-yellow-600" : "",
            ].join(" ")}
          >
            {value}
          </span>
          {alert && <AlertTriangle className="h-4 w-4 text-destructive mb-1" />}
        </div>
        {sub && (
          <p className="mt-1 text-xs text-muted-foreground">{sub}</p>
        )}
      </CardContent>
    </Card>
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
      <p className="mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wide">
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
                className={`w-full rounded-t ${color} transition-all`}
                style={{ height: `${h}%` }}
              />
              {/* tooltip */}
              <span className="pointer-events-none absolute bottom-full mb-1 hidden group-hover:block whitespace-nowrap rounded bg-popover px-1.5 py-0.5 text-xs shadow border border-border text-popover-foreground">
                {formatBucketLabel(p.bucket_start, bucket)}: {fmt(values[i])}
              </span>
            </div>
          );
        })}
      </div>
      {/* x-axis labels — show first, middle, last only to avoid crowding */}
      {points.length > 0 && (
        <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
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
  /** numerator key */
  numKey: keyof TimeseriesPoint;
  /** denominator key */
  denKey: keyof TimeseriesPoint;
  label: string;
  /** 0–1 threshold to draw a horizontal reference line */
  threshold?: number;
  thresholdLabel?: string;
  /** CSS color value (hex, hsl, named) used for the bars */
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
  fillColor = "#fb923c", // orange-400
}: RateLineChartProps) {
  const CHART_H = 128; // px — matches h-32

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

  // Use a ceiling that is the greater of max rate or 2× threshold (so the threshold line is
  // always visible and not at the top of the chart).
  const maxRate = Math.max(...rates, threshold ? threshold * 2 : 0.01);

  // Map rate → SVG y coordinate (0 = top, CHART_H = bottom)
  const toY = (r: number) => CHART_H - (r / maxRate) * CHART_H;

  const W = 400; // viewBox width
  const barW = W / Math.max(points.length, 1);

  return (
    <div>
      <p className="mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wide">
        {label}
      </p>
      <div className="relative w-full overflow-hidden" style={{ height: CHART_H }}>
        <svg
          viewBox={`0 0 ${W} ${CHART_H}`}
          preserveAspectRatio="none"
          className="w-full h-full"
        >
          {/* threshold reference line */}
          {threshold !== undefined && (
            <line
              x1={0}
              x2={W}
              y1={toY(threshold)}
              y2={toY(threshold)}
              strokeDasharray="4 3"
              strokeWidth={1.5}
              className={`stroke-destructive`}
            />
          )}
          {/* bars */}
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
      {/* x-axis */}
      {points.length > 0 && (
        <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
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
        <p className="mt-1 text-[10px] text-destructive">
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
      <span className="flex items-center gap-1 text-xs text-green-600">
        <Wifi className="h-3 w-3" />
        Live
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="flex items-center gap-1 text-xs text-yellow-600">
        <WifiOff className="h-3 w-3" />
        Reconnecting…
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1 text-xs text-muted-foreground">
      <Wifi className="h-3 w-3 animate-pulse" />
      Connecting…
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

  // ------ Topline query (used as initial/fallback) ------
  const {
    data: toplineData,
    isLoading: toplineLoading,
    error: toplineError,
  } = useQuery({
    queryKey: ["topline", statsWindow, TENANT],
    queryFn: () => getTopline(statsWindow, TENANT),
  });

  // ------ Timeseries query ------
  const bucket = bucketForWindow(statsWindow);
  const {
    data: timeseriesData,
    isLoading: tsLoading,
    error: tsError,
  } = useQuery<TimeseriesOut>({
    queryKey: ["timeseries", statsWindow, bucket, TENANT],
    queryFn: () => getTimeseries(statsWindow, bucket, TENANT),
  });

  // ------ SSE live updates ------
  useEffect(() => {
    // Close any existing connection
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
      // Browser will auto-retry; we just update the indicator.
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [statsWindow]);

  // Prefer live data from SSE when available; fall back to query result.
  const topline: Topline | null =
    liveTopline ?? toplineData?.topline ?? null;

  const points: TimeseriesPoint[] = timeseriesData?.points ?? [];

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">Deliverability Dashboard</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Tenant: <span className="font-medium">{TENANT}</span>
          </p>
        </div>
        <div className="flex items-center gap-3">
          <SseIndicator status={sseStatus} />
          <WindowToggle value={statsWindow} onChange={setStatsWindow} />
        </div>
      </div>

      {/* Loading / error states for topline */}
      {toplineLoading && !topline && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}
      {toplineError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          Failed to load metrics: {(toplineError as Error).message}
        </div>
      )}

      {/* ---- Topline stat cards ---- */}
      {topline && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
            title="Open Rate (unique)"
            value={pct(topline.open_rate)}
            sub={`${fmt(topline.unique_opens)} unique opens`}
          />
          <StatCard
            title="Click Rate (unique)"
            value={pct(topline.click_rate)}
            sub={`${fmt(topline.unique_clicks)} unique clicks`}
          />
          <StatCard
            title="Bounce Rate"
            value={pct(topline.bounce_rate)}
            sub={`${fmt(topline.bounces)} bounces (total — hard/soft split not yet exposed)`}
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
          {/* Complaint rate badge if over threshold */}
          {topline.complaint_rate >= 0.001 && (
            <div className="sm:col-span-2 lg:col-span-1 flex items-center">
              <Badge variant="destructive" className="text-sm px-3 py-1">
                Complaint rate exceeds 0.1% — SES action risk
              </Badge>
            </div>
          )}
        </div>
      )}

      {/* ---- Charts ---- */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* Sends over time */}
        <Card>
          <CardHeader>
            <CardTitle>Sends Over Time</CardTitle>
          </CardHeader>
          <CardContent>
            {tsLoading ? (
              <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
                Loading…
              </div>
            ) : tsError ? (
              <div className="text-sm text-destructive">
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

        {/* Bounce + complaint rates over time */}
        <Card>
          <CardHeader>
            <CardTitle>Bounce & Complaint Rates Over Time</CardTitle>
          </CardHeader>
          <CardContent className="space-y-6">
            {tsLoading ? (
              <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
                Loading…
              </div>
            ) : tsError ? (
              <div className="text-sm text-destructive">
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
                  fillColor="#fb923c"
                />
                <RateLineChart
                  points={points}
                  bucket={bucket}
                  numKey="complaints"
                  denKey="sends"
                  label="Complaint Rate"
                  threshold={0.001}
                  thresholdLabel="SES complaint threshold"
                  fillColor="#f43f5e"
                />
              </>
            )}
          </CardContent>
        </Card>

        {/* Top campaigns by volume — endpoint not yet available */}
        <Card>
          <CardHeader>
            <CardTitle>Top 10 Campaigns by Volume</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex h-32 flex-col items-center justify-center gap-2 text-center text-sm text-muted-foreground">
              <span className="text-2xl">📊</span>
              <span>Coming soon</span>
              <span className="text-xs">
                {/* TODO: wire GET /api/stats/campaigns-volume once backend endpoint exists */}
                No backend endpoint for per-campaign volume yet.
              </span>
            </div>
          </CardContent>
        </Card>

        {/* SES reputation panel */}
        <Card>
          <CardHeader>
            <CardTitle>SES Reputation & Account Status</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex h-32 flex-col items-center justify-center gap-2 text-center text-sm text-muted-foreground">
              <span className="text-2xl">🛡️</span>
              <span className="font-medium text-foreground">Not yet wired</span>
              <span className="text-xs max-w-xs">
                {/*
                  TODO: expose GET /api/stats/ses-reputation (boto3
                  ses.get_account → SendingEnabled, SendingQuota,
                  ses.get_account_sending_enabled, Reputations via
                  SES Mailbox Simulator or SES Account Management API).
                  No data is fabricated here per spec constraint.
                */}
                Requires a backend endpoint that proxies
                SES account reputation data. No fake data shown.
              </span>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
