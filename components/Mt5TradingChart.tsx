import { useEffect, useMemo, useState } from "react";
import styles from "./Mt5TradingChart.module.css";

type Candle = { time: string; open: number; high: number; low: number; close: number };
type Tick = { symbol: string; bid: number; ask: number; time?: string; time_msc?: number };

type Props = { connectionId: string; symbol: string; timeframe: string; count?: number };

function apiPath(connectionId: string, symbol: string, timeframe: string, count: number) {
  return `/api/mt5/connections/${encodeURIComponent(connectionId)}/candles/${encodeURIComponent(symbol)}/${encodeURIComponent(timeframe)}?count=${count}`;
}

export default function Mt5TradingChart({ connectionId, symbol, timeframe, count = 200 }: Props) {
  const [candles, setCandles] = useState<Candle[]>([]);
  const [tick, setTick] = useState<Tick | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "empty" | "error">("loading");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    setMessage("");
    setCandles([]);
    setTick(null);

    fetch(apiPath(connectionId, symbol, timeframe, count), { credentials: "same-origin", signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Chart request failed (${response.status})`);
        return response.json();
      })
      .then(payload => {
        if (controller.signal.aborted) return;
        const rows = Array.isArray(payload?.candles) ? payload.candles : [];
        setCandles(rows);
        setState(rows.length ? "ready" : "empty");
      })
      .catch(error => {
        if (!controller.signal.aborted) { setState("error"); setMessage(error instanceof Error ? error.message : "Unable to load market data."); }
      });

    return () => controller.abort();
  }, [connectionId, symbol, timeframe, count]);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const response = await fetch(`/api/mt5/connections/${encodeURIComponent(connectionId)}/tick/${encodeURIComponent(symbol)}`, { credentials: "same-origin", signal: controller.signal });
        if (!response.ok) return;
        const value = await response.json();
        if (active) setTick(value);
      } catch { /* disconnected state is represented by the last known tick */ }
    };
    void poll();
    const timer = window.setInterval(poll, 5000);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [connectionId, symbol]);

  const points = useMemo(() => {
    if (!candles.length) return [];
    const width = 900, height = 360, pad = 24;
    const low = Math.min(...candles.map(c => c.low));
    const high = Math.max(...candles.map(c => c.high));
    const range = high - low || 1;
    return candles.map((c, index) => {
      const x = pad + (index / Math.max(candles.length - 1, 1)) * (width - pad * 2);
      const y = (value: number) => height - pad - ((value - low) / range) * (height - pad * 2);
      return { x, open: y(c.open), close: y(c.close), high: y(c.high), low: y(c.low), rising: c.close >= c.open };
    });
  }, [candles]);

  if (state === "loading") return <div className={styles.chartState}>Loading managed market data…</div>;
  if (state === "error") return <div className={`${styles.chartState} ${styles.chartError}`}>{message}</div>;
  if (state === "empty") return <div className={styles.chartState}>No candle history is available for this selection.</div>;

  return <section aria-label={`${symbol} ${timeframe} managed MT5 chart`}>
    <div className={styles.chartToolbar}>
      <span>{symbol} · {timeframe}</span>
      {tick ? <span>Bid {tick.bid} · Ask {tick.ask}</span> : <span>Live tick unavailable</span>}
    </div>
    <svg className={styles.chart} viewBox="0 0 900 360" role="img" aria-label="Candlestick chart">
      {points.map((point, index) => <g key={`${candles[index].time}-${index}`}>
        <line x1={point.x} x2={point.x} y1={point.high} y2={point.low} stroke={point.rising ? "#16a34a" : "#dc2626"} />
        <rect x={point.x - 2} y={Math.min(point.open, point.close)} width="4" height={Math.max(Math.abs(point.close - point.open), 1)} fill={point.rising ? "#16a34a" : "#dc2626"} />
      </g>)}
    </svg>
  </section>;
}
