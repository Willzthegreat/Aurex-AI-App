const crypto = require('crypto');
const { spawn, execFileSync } = require('child_process');

const token = crypto.randomBytes(32).toString('hex');
const env = { ...process.env, AUREX_CONNECTOR_SERVICE_TOKEN: token };

const service = spawn('python', ['-m', 'uvicorn', 'mt5_data_service:app', '--host', '127.0.0.1', '--port', '8010'], {
  cwd: __dirname,
  env,
  stdio: ['ignore', 'pipe', 'pipe'],
});
service.stderr.on('data', data => process.stderr.write(data));
service.stdout.on('data', data => process.stdout.write(data));

const tunnel = spawn('C:\\PROGRA~2\\cloudflared\\cloudflared.exe', ['tunnel', '--url', 'http://127.0.0.1:8010', '--no-autoupdate'], {
  cwd: __dirname,
  env,
  stdio: ['ignore', 'pipe', 'pipe'],
});

let output = '';
let publicUrl;
const consume = data => {
  const text = data.toString();
  output += text;
  process.stderr.write(text.replace(token, '[redacted]'));
  const match = output.match(/https:\/\/[-a-z0-9]+\.trycloudflare\.com/);
  if (match && !publicUrl) {
    publicUrl = match[0];
    verify();
  }
};
tunnel.stdout.on('data', consume);
tunnel.stderr.on('data', consume);

async function verify() {
  for (let i = 0; i < 30; i++) {
    try {
      const health = await fetch(`${publicUrl}/health`, { headers: { Authorization: `Bearer ${token}` } });
      const tick = await fetch(`${publicUrl}/api/tick/XAUUSDm`, { headers: { Authorization: `Bearer ${token}` } });
      const candles = await fetch(`${publicUrl}/api/candles/XAUUSDm/M1`, { headers: { Authorization: `Bearer ${token}` } });
      if (health.ok && tick.ok && candles.ok) {
        try {
          execFileSync('setx', ['AUREX_CONNECTOR_SERVICE_TOKEN', token], { stdio: 'ignore' });
        } catch {}
        console.log(`QUICK_TUNNEL_URL=${publicUrl}`);
        console.log(`AUTH_HEALTH=${health.status}`);
        console.log(`AUTH_TICK=${tick.status}`);
        console.log(`AUTH_CANDLES=${candles.status}`);
        console.log('TOKEN=redacted');
        console.log('CONNECTOR_AND_TUNNEL_RUNNING=1');
        return;
      }
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  console.error('Public endpoint did not pass authenticated verification.');
}

process.on('SIGINT', () => {
  service.kill();
  tunnel.kill();
  process.exit(0);
});
