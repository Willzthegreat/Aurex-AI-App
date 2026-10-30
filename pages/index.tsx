// import { useEffect, useState } from "react";
// import { useRouter } from "next/router";
// import Mt5TradingChart from "../components/Mt5TradingChart";

// type Connection = { connection_id: string; label: string; server_name: string; status: string; revoked?: number };
// type User = { id: string; email: string };

// export default function Home() {
//   const router = useRouter();
//   const [user, setUser] = useState<User | null>(null);
//   const [checkingSession, setCheckingSession] = useState(true);
//   const [connections, setConnections] = useState<Connection[]>([]);
//   const [connectionId, setConnectionId] = useState("");
//   const [symbol, setSymbol] = useState("XAUUSDm");
//   const [timeframe, setTimeframe] = useState("M15");
//   const [error, setError] = useState("");

//   useEffect(() => {
//     const sessionController = new AbortController();
//     fetch("/api/auth/me", { credentials: "same-origin", signal: sessionController.signal })
//       .then(async response => {
//         if (response.status === 401) { await router.replace("/login"); return null; }
//         if (!response.ok) throw new Error("Unable to verify the session.");
//         return response.json();
//       })
//       .then(payload => { if (payload?.user) setUser(payload.user); })
//       .catch(reason => { if (!sessionController.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to verify the session."); })
//       .finally(() => { if (!sessionController.signal.aborted) setCheckingSession(false); });
//     return () => sessionController.abort();
//   }, [router]);

//   useEffect(() => {
//     if (checkingSession || !user) return;
//     const controller = new AbortController();
//     fetch("/api/mt5/connections", { credentials: "same-origin", signal: controller.signal })
//       .then(async response => {
//         if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Unable to load connections (${response.status})`);
//         return response.json();
//       })
//       .then(payload => {
//         if (controller.signal.aborted) return;
//         const available = (payload.connections || []).filter((item: Connection) => !item.revoked && item.status !== "REVOKED");
//         setConnections(available);
//         setConnectionId(current => current || available[0]?.connection_id || "");
//       })
//       .catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to load connections."); });
//     return () => controller.abort();
//   }, [checkingSession, user]);

//   async function signOut() {
//     await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
//     setUser(null);
//     await router.replace("/login");
//   }

//   if (checkingSession) return <main style={{ maxWidth: 1100, margin: "40px auto", padding: "0 20px", fontFamily: "system-ui, sans-serif" }}><p>Checking your session…</p></main>;
//   if (!user) return null;

//   return <main style={{ maxWidth: 1100, margin: "40px auto", padding: "0 20px", fontFamily: "system-ui, sans-serif" }}>
//     <header style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
//       <div><h1>Aurex MT5 Market Data</h1><p>Signed in as {user.email}</p></div>
//       <button type="button" onClick={signOut}>Sign out</button>
//     </header>
//     {error && <p role="alert">{error}</p>}
//     {!connections.length && !error && <p>No authorized managed MT5 connections are available.</p>}
//     {!!connections.length && <>
//       <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 20 }}>
//         <label>Connection <select value={connectionId} onChange={event => setConnectionId(event.target.value)}>{connections.map(item => <option key={item.connection_id} value={item.connection_id}>{item.label} ({item.status})</option>)}</select></label>
//         <label>Symbol <input value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
//         <label>Timeframe <select value={timeframe} onChange={event => setTimeframe(event.target.value)}>{["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"].map(item => <option key={item}>{item}</option>)}</select></label>
//       </div>
//       {connectionId && <Mt5TradingChart connectionId={connectionId} symbol={symbol} timeframe={timeframe} />}
//     </>}
//   </main>;
// }










import { useEffect, useState } from "react";
import { useRouter } from "next/router";
import Mt5TradingChart from "../components/Mt5TradingChart";

type Connection = {
  connection_id: string;
  label: string;
  server_name: string;
  status: string;
  revoked?: number | boolean;
};

type User = {
  id: string;
  email: string;
};

type BrokerSymbol = {
  symbol: string;
  description?: string;
  visible?: boolean;
};

type SymbolsResponse = {
  symbols: BrokerSymbol[];
};

const DEFAULT_SYMBOL = "XAUUSDm";

const TIMEFRAMES = [
  "M1",
  "M5",
  "M15",
  "M30",
  "H1",
  "H4",
  "D1",
  "W1",
  "MN1",
];

export default function Home() {
  const router = useRouter();

  const [user, setUser] = useState<User | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);

  const [connections, setConnections] = useState<Connection[]>([]);
  const [connectionId, setConnectionId] = useState("");

  const [symbols, setSymbols] = useState<BrokerSymbol[]>([]);
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [loadingSymbols, setLoadingSymbols] = useState(false);
  const [symbolError, setSymbolError] = useState("");

  const [timeframe, setTimeframe] = useState("M15");
  const [error, setError] = useState("");
  const [executionMode, setExecutionMode] = useState<"DEMO" | "LIVE">("DEMO");
  const [orderSide, setOrderSide] = useState<"BUY" | "SELL">("BUY");
  const [orderVolume, setOrderVolume] = useState("0.01");
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  const [executionStatus, setExecutionStatus] = useState("DISCONNECTED");
  const [executionMessage, setExecutionMessage] = useState("");
  const [submittingOrder, setSubmittingOrder] = useState(false);

  useEffect(() => {
    if (!connectionId || !user) {
      setExecutionStatus("DISCONNECTED");
      return;
    }
    let cancelled = false;
    setExecutionStatus("CONNECTING");
    fetch(`/api/mt5/connections/${encodeURIComponent(connectionId)}/execution-readiness?mode=${executionMode}`, { credentials: "same-origin" })
      .then(async response => response.json().catch(() => ({})))
      .then(payload => {
        if (cancelled) return;
        setExecutionStatus(String(payload.status || "DISCONNECTED"));
        setExecutionMessage(payload.error || "");
      })
      .catch(() => { if (!cancelled) setExecutionStatus("DISCONNECTED"); });
    return () => { cancelled = true; };
  }, [connectionId, executionMode, user]);

  async function submitBrokerOrder() {
    if (!connectionId || !symbol || executionMode !== "DEMO") return;
    setSubmittingOrder(true);
    setExecutionStatus("EXECUTING");
    setExecutionMessage("");
    try {
      const response = await fetch("/api/mt5/orders", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          connection_id: connectionId,
          mode: executionMode,
          symbol,
          side: orderSide,
          volume: Number(orderVolume),
          stop_loss: stopLoss ? Number(stopLoss) : undefined,
          take_profit: takeProfit ? Number(takeProfit) : undefined,
          client_order_key: `manual-${crypto.randomUUID()}`,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "The broker rejected the order request.");
      const execution = payload.execution || {};
      setExecutionStatus(String(execution.status || "UNKNOWN"));
      setExecutionMessage(execution.result?.broker_comment || execution.result?.error || "Broker response received.");
    } catch (reason) {
      setExecutionStatus("ORDER REJECTED");
      setExecutionMessage(reason instanceof Error ? reason.message : "Unable to submit the order.");
    } finally {
      setSubmittingOrder(false);
    }
  }

  // Verify the user's login session.
  useEffect(() => {
    const controller = new AbortController();

    async function checkSession() {
      try {
        const response = await fetch("/api/auth/me", {
          credentials: "same-origin",
          signal: controller.signal,
        });

        if (response.status === 401) {
          await router.replace("/login");
          return;
        }

        if (!response.ok) {
          throw new Error("Unable to verify the session.");
        }

        const payload = await response.json();

        if (payload?.user) {
          setUser(payload.user);
        } else {
          await router.replace("/login");
        }
      } catch (reason) {
        if (controller.signal.aborted) return;

        setError(
          reason instanceof Error
            ? reason.message
            : "Unable to verify the session."
        );
      } finally {
        if (!controller.signal.aborted) {
          setCheckingSession(false);
        }
      }
    }

    void checkSession();

    return () => controller.abort();
  }, [router]);

  // Load the authenticated user's managed MT5 connections.
  useEffect(() => {
    if (checkingSession || !user) return;

    const controller = new AbortController();

    async function loadConnections() {
      try {
        const response = await fetch("/api/mt5/connections", {
          credentials: "same-origin",
          signal: controller.signal,
        });

        const payload = await response.json().catch(() => ({}));

        if (!response.ok) {
          throw new Error(
            payload.detail ||
              `Unable to load connections (${response.status}).`
          );
        }

        if (controller.signal.aborted) return;

        const available: Connection[] = (
          payload.connections || []
        ).filter(
          (item: Connection) =>
            !item.revoked && item.status !== "REVOKED"
        );

        setConnections(available);

        setConnectionId((current) => {
          if (
            current &&
            available.some((item) => item.connection_id === current)
          ) {
            return current;
          }

          return available[0]?.connection_id || "";
        });
      } catch (reason) {
        if (controller.signal.aborted) return;

        setError(
          reason instanceof Error
            ? reason.message
            : "Unable to load connections."
        );
      }
    }

    void loadConnections();

    return () => controller.abort();
  }, [checkingSession, user]);

  // Retrieve the symbols supported by the selected broker connection.
  useEffect(() => {
    if (!connectionId || !user) {
      setSymbols([]);
      setSymbolError("");
      return;
    }

    const controller = new AbortController();

    async function loadSymbols() {
      setLoadingSymbols(true);
      setSymbolError("");
      setSymbols([]);

      try {
        const response = await fetch(
          `/api/mt5/connections/${encodeURIComponent(
            connectionId
          )}/symbols`,
          {
            credentials: "same-origin",
            signal: controller.signal,
          }
        );

        const payload = (await response.json().catch(() => ({}))) as
          | SymbolsResponse
          | { detail?: string };

        if (!response.ok) {
          throw new Error(
            "detail" in payload && payload.detail
              ? payload.detail
              : `Unable to load broker symbols (${response.status}).`
          );
        }

        if (!("symbols" in payload) || !Array.isArray(payload.symbols)) {
          throw new Error("The broker returned an invalid symbol list.");
        }

        const available = payload.symbols
          .filter(
            (item): item is BrokerSymbol =>
              !!item &&
              typeof item.symbol === "string" &&
              /^[A-Za-z0-9._-]{1,32}$/.test(item.symbol)
          )
          .sort((a, b) => {
            if (Boolean(a.visible) !== Boolean(b.visible)) {
              return a.visible ? -1 : 1;
            }

            return a.symbol.localeCompare(b.symbol);
          });

        if (controller.signal.aborted) return;

        setSymbols(available);

        setSymbol((current) => {
          // Keep the selected instrument when switching connections if
          // that instrument is also available on the new connection.
          if (available.some((item) => item.symbol === current)) {
            return current;
          }

          // Prefer the configured default if the broker provides it.
          const defaultMatch = available.find(
            (item) => item.symbol === DEFAULT_SYMBOL
          );

          // Otherwise select the first instrument returned by the broker.
          return defaultMatch?.symbol || available[0]?.symbol || "";
        });

        if (available.length === 0) {
          setSymbolError(
            "No symbols were returned by the broker. Check the MT5 connection and Market Watch."
          );
        }
      } catch (reason) {
        if (controller.signal.aborted) return;

        setSymbols([]);
        setSymbol("");

        setSymbolError(
          reason instanceof Error
            ? reason.message
            : "Unable to retrieve symbols from the broker."
        );
      } finally {
        if (!controller.signal.aborted) {
          setLoadingSymbols(false);
        }
      }
    }

    void loadSymbols();

    return () => controller.abort();
  }, [connectionId, user]);

  async function signOut() {
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "same-origin",
      });
    } finally {
      setUser(null);
      await router.replace("/login");
    }
  }

  if (checkingSession) {
    return (
      <main
        style={{
          maxWidth: 1100,
          margin: "40px auto",
          padding: "0 20px",
          fontFamily: "system-ui, sans-serif",
        }}
      >
        <p>Checking your session…</p>
      </main>
    );
  }

  if (!user) return null;

  return (
    <main
      style={{
        maxWidth: 1100,
        margin: "40px auto",
        padding: "0 20px",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <header
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 16,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <div>
          <h1>Klyrotrader MT5 Market Data</h1>
          <p>Signed in as {user.email}</p>
        </div>

        <button type="button" onClick={signOut}>
          Sign out
        </button>
      </header>

      {error && <p role="alert">{error}</p>}

      {!connections.length && !error && (
        <p>No authorized managed MT5 connections are available.</p>
      )}

      {!!connections.length && (
        <>
          <div
            style={{
              display: "flex",
              gap: 12,
              flexWrap: "wrap",
              marginBottom: 20,
              alignItems: "center",
            }}
          >
            <label>
              Connection{" "}
              <select
                value={connectionId}
                onChange={(event) => setConnectionId(event.target.value)}
              >
                {connections.map((item) => (
                  <option
                    key={item.connection_id}
                    value={item.connection_id}
                  >
                    {item.label} ({item.status})
                  </option>
                ))}
              </select>
            </label>

            <label>
              Symbol{" "}
              <select
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
                disabled={loadingSymbols || symbols.length === 0}
              >
                {loadingSymbols && <option value="">Loading symbols…</option>}

                {!loadingSymbols && symbols.length === 0 && (
                  <option value="">No symbols available</option>
                )}

                {symbols.map((item) => (
                  <option key={item.symbol} value={item.symbol}>
                    {item.symbol}
                    {item.description
                      ? ` — ${item.description}`
                      : ""}
                  </option>
                ))}
              </select>
            </label>

            <label>
              Timeframe{" "}
              <select
                value={timeframe}
                onChange={(event) => setTimeframe(event.target.value)}
              >
                {TIMEFRAMES.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {loadingSymbols && (
            <p aria-live="polite">Loading instruments from MT5…</p>
          )}

          {symbolError && <p role="alert">{symbolError}</p>}

          <section style={{ border: "1px solid #d8dee9", borderRadius: 8, padding: 16, marginBottom: 20 }} aria-label="Broker order execution">
            <h2 style={{ marginTop: 0 }}>Broker execution</h2>
            <p aria-live="polite">Status: <strong>{executionStatus}</strong>{executionMessage ? ` — ${executionMessage}` : ""}</p>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
              <label>Mode <select value={executionMode} onChange={event => setExecutionMode(event.target.value as "DEMO" | "LIVE")}><option value="DEMO">DEMO</option><option value="LIVE">LIVE</option></select></label>
              <label>Direction <select value={orderSide} onChange={event => setOrderSide(event.target.value as "BUY" | "SELL")}><option value="BUY">BUY</option><option value="SELL">SELL</option></select></label>
              <label>Volume <input inputMode="decimal" value={orderVolume} onChange={event => setOrderVolume(event.target.value)} /></label>
              <label>Stop-loss <input inputMode="decimal" value={stopLoss} onChange={event => setStopLoss(event.target.value)} placeholder="Optional" /></label>
              <label>Take-profit <input inputMode="decimal" value={takeProfit} onChange={event => setTakeProfit(event.target.value)} placeholder="Optional" /></label>
              <button type="button" onClick={() => void submitBrokerOrder()} disabled={submittingOrder || executionMode !== "DEMO" || executionStatus !== "DEMO_READY" || !symbol}>{submittingOrder ? "Executing…" : "Review and submit DEMO order"}</button>
            </div>
            {executionMode === "LIVE" && <p>LIVE execution is disabled by server policy until independently authorized.</p>}
            <p style={{ fontSize: 13 }}>Exact selected symbol: <code>{symbol || "none"}</code>. Orders are sent only to the selected managed MT5 connection.</p>
          </section>

          {connectionId && symbol && !symbolError && (
            <Mt5TradingChart
              connectionId={connectionId}
              symbol={symbol}
              timeframe={timeframe}
            />
          )}
        </>
      )}
    </main>
  );
}

