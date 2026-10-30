import { useEffect, useMemo, useRef, useState } from "react";
import styles from "./Mt5TradingChart.module.css";

type Candle = { time: string; open: number; high: number; low: number; close: number };
type Tick = { symbol: string; bid: number; ask: number; time?: string; time_msc?: number };
type Props = { connectionId: string; symbol: string; timeframe: string; count?: number };

function apiPath(connectionId: string, symbol: string, timeframe: string, count: number) {
  return `/api/mt5/connections/${encodeURIComponent(connectionId)}/candles/${encodeURIComponent(symbol)}/${encodeURIComponent(timeframe)}?count=${count}`;
}

function normalizeCandles(value: unknown): Candle[] {
  if (!Array.isArray(value)) return [];
  const byTime = new Map<string, Candle>();
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    if (typeof row.time !== "string" || !row.time) continue;
    const open = Number(row.open);
    const high = Number(row.high);
    const low = Number(row.low);
    const close = Number(row.close);
    if (![open, high, low, close].every(Number.isFinite)) continue;
    byTime.set(row.time, { time: row.time, open, high, low, close });
  }
  return Array.from(byTime.values()).sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
}

export default function Mt5TradingChart({ connectionId, symbol, timeframe, count = 200 }: Props) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const [chartWidth, setChartWidth] = useState(900);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [tick, setTick] = useState<Tick | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "empty" | "error">("loading");
  const [message, setMessage] = useState("");

  // Re-measure the chart itself so orientation changes and responsive parent
  // layouts always leave room for the complete right-hand price scale.
  useEffect(() => {
    const element = chartContainerRef.current;
    if (!element) return;

    const resize = () => setChartWidth(Math.max(320, Math.round(element.clientWidth)));
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    window.addEventListener("orientationchange", resize);
    return () => {
      observer.disconnect();
      window.removeEventListener("orientationchange", resize);
    };
  }, []);

  // Reload immediately when the connection, pair, timeframe, or candle count changes.
  // Then refresh the active candle history so the chart reflects live market movement.
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let timeoutId: number | undefined;
    let hasLoaded = false;

    setState("loading");
    setMessage("");
    setCandles([]);
    setTick(null);

    const loadCandles = async () => {
      try {
        const response = await fetch(apiPath(connectionId, symbol, timeframe, count), {
          credentials: "same-origin",
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body?.detail || `Chart request failed (${response.status})`);
        }

        const payload = await response.json();
        if (!active || controller.signal.aborted) return;
        if (payload?.symbol && payload.symbol !== symbol) {
          throw new Error(`Market data mismatch: requested ${symbol}, received ${payload.symbol}.`);
        }
        if (payload?.timeframe && String(payload.timeframe).toUpperCase() !== timeframe.toUpperCase()) {
          throw new Error(`Timeframe mismatch: requested ${timeframe}, received ${payload.timeframe}.`);
        }

        const rows = normalizeCandles(payload?.candles);
        if (!rows.length) {
          if (!hasLoaded) {
            setCandles([]);
            setState("empty");
          }
        } else {
          setCandles(rows.slice(-count));
          setState("ready");
          setMessage("");
        }
        hasLoaded = true;
      } catch (error) {
        if (!active || controller.signal.aborted) return;
        const text = error instanceof Error ? error.message : "Unable to load market data.";
        if (!hasLoaded) {
          setState("error");
          setMessage(text);
        } else {
          setMessage(`Live candle refresh failed: ${text}`);
        }
      } finally {
        if (active && !controller.signal.aborted) {
          timeoutId = window.setTimeout(loadCandles, 10000);
        }
      }
    };

    void loadCandles();
    return () => {
      active = false;
      controller.abort();
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
  }, [connectionId, symbol, timeframe, count]);

  // Poll ticks for the currently selected pair only; never show a previous pair's tick.
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setTick(null);

    const poll = async () => {
      try {
        const response = await fetch(
          `/api/mt5/connections/${encodeURIComponent(connectionId)}/tick/${encodeURIComponent(symbol)}`,
          { credentials: "same-origin", signal: controller.signal, cache: "no-store" }
        );
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          if (active && !controller.signal.aborted && body?.detail) setMessage(String(body.detail));
          return;
        }
        const value = await response.json();
        if (!active || controller.signal.aborted) return;
        if (value?.symbol !== symbol || !Number.isFinite(Number(value?.bid)) || !Number.isFinite(Number(value?.ask))) return;
        setTick({ ...value, bid: Number(value.bid), ask: Number(value.ask) });
      } catch {
        // Keep the current pair's last valid tick while the connector recovers.
      }
    };

    void poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      active = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [connectionId, symbol]);

  const chart = useMemo(() => {
    if (!candles.length) return null;
    const height = 360;
    const priceScaleWidth = Math.min(120, Math.max(96, chartWidth * 0.24));
    const plotWidth = Math.max(220, chartWidth - priceScaleWidth);
    const pad = 24;
    const low = Math.min(...candles.map(c => c.low));
    const high = Math.max(...candles.map(c => c.high));
    const range = high - low || 1;
    const decimals = Math.min(8, Math.max(0, ...candles.map(c => {
      const text = String(c.close);
      return text.includes(".") ? text.length - text.indexOf(".") - 1 : 0;
    })));
    const formatPrice = (value: number) => value.toFixed(decimals);
    const y = (value: number) => height - pad - ((value - low) / range) * (height - pad * 2);
    const ticks = Array.from({ length: 7 }, (_, index) => {
      const value = high - (range * index) / 6;
      return { value, y: y(value), label: formatPrice(value) };
    });
    const points = candles.map((c, index) => {
      const x = pad + (index / Math.max(candles.length - 1, 1)) * (plotWidth - pad * 2);
      return { x, open: y(c.open), close: y(c.close), high: y(c.high), low: y(c.low), rising: c.close >= c.open };
    });
    return { height, low, high, plotWidth, priceScaleWidth, ticks, points };
  }, [candles, chartWidth]);

  if (state === "loading") return <div className={styles.chartState}>Loading {symbol} {timeframe} market data…</div>;
  if (state === "error") return <div className={`${styles.chartState} ${styles.chartError}`}>{message}</div>;
  if (state === "empty") return <div className={styles.chartState}>No candle history is available for {symbol} on {timeframe}. Check the exact symbol offered by this broker.</div>;

  return <section aria-label={`${symbol} ${timeframe} managed MT5 chart`} data-symbol={symbol} data-timeframe={timeframe}>
    <div className={styles.chartToolbar}>
      <span>{symbol} · {timeframe}</span>
      {tick ? <span>Bid {tick.bid} · Ask {tick.ask}</span> : <span>Waiting for {symbol} live tick…</span>}
    </div>
    {message && <p role="status">{message}</p>}
    <div ref={chartContainerRef} className={styles.chartContainer}>
      <svg className={styles.chart} viewBox={`0 0 ${chart ? chart.plotWidth + chart.priceScaleWidth : chartWidth} ${chart ? chart.height : 360}`} role="img" aria-label={`${symbol} ${timeframe} candlestick chart`}>
      {chart && <>
        <g className={styles.priceGrid} aria-hidden="true">
          {chart.ticks.map(tick => <line key={`grid-${tick.label}`} x1="0" x2={chart.plotWidth} y1={tick.y} y2={tick.y} />)}
        </g>
        <g className={styles.priceScale} aria-label="Price scale">
          <line x1={chart.plotWidth} x2={chart.plotWidth} y1="0" y2={chart.height} />
          {chart.ticks.map(tick => <text key={`label-${tick.label}`} x={chart.plotWidth + chart.priceScaleWidth - 8} y={tick.y + 4} textAnchor="end">{tick.label}</text>)}
        </g>
      </>}
      {chart?.points.map((point, index) => <g key={`${symbol}-${timeframe}-${candles[index].time}`}>
        <line x1={point.x} x2={point.x} y1={point.high} y2={point.low} stroke={point.rising ? "#16a34a" : "#dc2626"} />
        <rect x={point.x - 2} y={Math.min(point.open, point.close)} width="4" height={Math.max(Math.abs(point.close - point.open), 1)} fill={point.rising ? "#16a34a" : "#dc2626"} />
      </g>)}
      </svg>
    </div>
    <p>Last candle: {candles[candles.length - 1]?.time ?? "—"} · Auto-refreshes every 10 seconds</p>
  </section>;
}
