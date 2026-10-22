import { useEffect, useState } from "react";
import { useRouter } from "next/router";
import Mt5TradingChart from "../components/Mt5TradingChart";

type Connection = { connection_id: string; label: string; server_name: string; status: string; revoked?: number };
type User = { id: string; email: string };

export default function Home() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [symbol, setSymbol] = useState("XAUUSDm");
  const [timeframe, setTimeframe] = useState("M15");
  const [error, setError] = useState("");

  useEffect(() => {
    const sessionController = new AbortController();
    fetch("/api/auth/me", { credentials: "same-origin", signal: sessionController.signal })
      .then(async response => {
        if (response.status === 401) { await router.replace("/login"); return null; }
        if (!response.ok) throw new Error("Unable to verify the session.");
        return response.json();
      })
      .then(payload => { if (payload?.user) setUser(payload.user); })
      .catch(reason => { if (!sessionController.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to verify the session."); })
      .finally(() => { if (!sessionController.signal.aborted) setCheckingSession(false); });
    return () => sessionController.abort();
  }, [router]);

  useEffect(() => {
    if (checkingSession || !user) return;
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
  }, [checkingSession, user]);

  async function signOut() {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    setUser(null);
    await router.replace("/login");
  }

  if (checkingSession) return <main style={{ maxWidth: 1100, margin: "40px auto", padding: "0 20px", fontFamily: "system-ui, sans-serif" }}><p>Checking your session…</p></main>;
  if (!user) return null;

  return <main style={{ maxWidth: 1100, margin: "40px auto", padding: "0 20px", fontFamily: "system-ui, sans-serif" }}>
    <header style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
      <div><h1>Aurex MT5 Market Data</h1><p>Signed in as {user.email}</p></div>
      <button type="button" onClick={signOut}>Sign out</button>
    </header>
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
