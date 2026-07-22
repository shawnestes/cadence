import { apiFetch } from "@/api/client";
import type {
  CampaignsVolumeOut,
  IspReputationOut,
  SESReputationOut,
  StatsBucket,
  StatsWindow,
  TimeseriesOut,
  ToplineWindow,
} from "@/api/types";

export function getTopline(
  window: StatsWindow = "24h",
  tenant = "otto",
): Promise<ToplineWindow> {
  return apiFetch<ToplineWindow>(
    `/api/stats/topline?window=${encodeURIComponent(window)}&tenant=${encodeURIComponent(tenant)}`,
  );
}

export function getTimeseries(
  window: StatsWindow = "24h",
  bucket: StatsBucket = "1h",
  tenant = "otto",
): Promise<TimeseriesOut> {
  return apiFetch<TimeseriesOut>(
    `/api/stats/timeseries?window=${encodeURIComponent(window)}&bucket=${encodeURIComponent(bucket)}&tenant=${encodeURIComponent(tenant)}`,
  );
}

export function getCampaignsVolume(
  window: StatsWindow = "24h",
  tenant = "otto",
  limit = 10,
): Promise<CampaignsVolumeOut> {
  return apiFetch<CampaignsVolumeOut>(
    `/api/stats/campaigns-volume?window=${encodeURIComponent(window)}&tenant=${encodeURIComponent(tenant)}&limit=${limit}`,
  );
}

export function getSesReputation(tenant = "otto"): Promise<SESReputationOut> {
  return apiFetch<SESReputationOut>(
    `/api/stats/ses-reputation?tenant=${encodeURIComponent(tenant)}`,
  );
}

export function getIspReputation(tenant = "otto"): Promise<IspReputationOut> {
  return apiFetch<IspReputationOut>(
    `/api/stats/isp-reputation?tenant=${encodeURIComponent(tenant)}`,
  );
}

// SSE helper — returns the EventSource and lets caller wire onmessage handlers.
// EventSource cannot set custom headers, so we pass the API key via query param.
export function streamTopline(
  tenant = "otto",
  window: StatsWindow = "24h",
): EventSource {
  const apiKey = import.meta.env.VITE_API_KEY ?? "";
  const base = import.meta.env.VITE_API_BASE_URL ?? "";
  const url = `${base}/api/stats/stream?tenant=${encodeURIComponent(tenant)}&window=${encodeURIComponent(window)}&api_key=${encodeURIComponent(apiKey)}`;
  return new EventSource(url);
}
