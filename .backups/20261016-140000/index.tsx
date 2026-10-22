import { useEffect, useState } from "react";
import Mt5TradingChart from "../components/Mt5TradingChart";

type Connection = { connection_id: string; label: string; server_name: string; status: string; revoked?: number };

export default function Home() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [symbol, setSymbol] = useState("XAUUSDm");
  const [timeframe, setTimeframe] = useState("M15");
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/mt5/connections", { credentials: "same-origin", signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Unable to load connections (${response.status})`);
        return response.json();
      })
      .then(payload => {
        if (controller.signal.aborted) return;
        const available = (payload.connections || []).filter((item: Connection) => !item.revoked && item.status !== "REVOKED");
        setConnections(available);
        setConnectionId(current => current || available[0]?.connection_id || "");
      })
      .catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to load connections."); });
    return () => controller.abort();
  }, []);

  return <main style={{ maxWidth: 1100, margin: "40px auto", padding: "0 20px", fontFamily: "system-ui, sans-serif" }}>
    <h1>Aurex MT5 Market Data</h1>
    {error && <p role="alert">{error}</p>}
    {!connections.length && !error && <p>No authorized managed MT5 connections are available.</p>}
    {!!connections.length && <>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 20 }}>
        <label>Connection <select value={connectionId} onChange={event => setConnectionId(event.target.value)}>{connections.map(item => <option key={item.connection_id} value={item.connection_id}>{item.label} ({item.status})</option>)}</select></label>
        <label>Symbol <input value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
        <label>Timeframe <select value={timeframe} onChange={event => setTimeframe(event.target.value)}>{["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"].map(item => <option key={item}>{item}</option>)}</select></label>
      </div>
      {connectionId && <Mt5TradingChart connectionId={connectionId} symbol={symbol} timeframe={timeframe} />}
    </>}
  </main>;
}
