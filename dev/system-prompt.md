Create a complete, production-ready AI Trading Application using my Smart Money Concepts (SMC) multi-timeframe strategy, integrated with Exness/MetaTrader 5.

Core features: professional responsive dark trading dashboard, real-time market data, interactive candlestick charts, AI market analysis, trade signals, risk management, trade journal, backtesting, paper trading, notifications, authentication, and performance analytics. Use a secure modular architecture with React/TypeScript, Node/server endpoints, database, and realtime updates.

Strategy:
HTF directional bias: market structure HH/HL/LH/LL/BOS/CHOCH; strong/weak highs and lows; premium/discount; trendline liquidity; supply/demand.
MTF confirmation: liquidity sweep, CHOCH, bullish/bearish divergence, RSI filter.
LTF execution: liquidity sweep, CHOCH, Fair Value Gap, Order Block, pinbar candle confirmation.
Configurable timeframes: Monthly, Weekly, Daily, H4, H1, M15, M5, M1. Each condition must have actual testable detection logic, explain pass/fail, and block trades if mandatory conditions are unmet.

Three modes using the same deterministic strategy and risk engines:
1. Automatic trading only after the user explicitly enables automation.
2. AI setup + confirmation: calculate entry, stop, target, position size, risk/reward, then wait for explicit approval.
3. AI setup + manual execution: explain setup and show parameters; never submit orders in this mode.

Exness/MT5: verify currently supported integration methods. Implement broker connectivity only where supported for account info, live prices, historical candles, positions, pending orders and execution. Never invent endpoints, expose credentials, or report execution without broker confirmation. Prevent duplicate orders and reconcile uncertain execution states.

Risk: configurable risk %, broker-accurate position sizing using symbol specifications/tick values/contract size/account currency, SL/TP, minimum R:R, daily loss, drawdown, max trades, spread and session filters. Block trades if calculations fail or limits are breached. Include emergency Kill Switch.

AI may explain conditions but deterministic logic authorizes trades. Backtesting must avoid look-ahead bias and reuse live strategy logic. Paper trading must simulate orders, costs, positions and stats, clearly separated from live trading.

Security/production: secure authentication and authorization, encrypted secrets, migrations, API validation, audit logs, error handling, monitoring, backups, automated tests and deployment configuration. Start with paper trading, validate Exness/MT5 integration, and enable live trading only after security/execution/risk tests pass. Never fabricate market data, broker connectivity, AI signals, or executions. Live automation disabled by default. Never claim production readiness while critical functionality is unimplemented or untested.