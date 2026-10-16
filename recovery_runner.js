// Legacy recovery runner disabled.
// The old version launched mt5_data_service on the managed connector port
// and exposed the single-account bridge through a temporary public tunnel.
// Start the isolated managed connector instead:
//   python start_managed_connector.py
console.error("Legacy MT5 bridge recovery is disabled. Use start_managed_connector.py on port 8010.");
process.exitCode = 1;
