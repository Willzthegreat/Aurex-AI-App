import { FormEvent, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/router";

export default function Login() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault(); setError("");
    if (!email.trim() || !password) { setError("Enter your email and password."); return; }
    setLoading(true);
    try {
      const response = await fetch("/api/auth/login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(response.status === 429 ? payload.detail : "Invalid email or password.");
      await router.replace("/");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to sign in."); }
    finally { setLoading(false); }
  }

  return <main style={{ maxWidth: 420, margin: "12vh auto", padding: 24, fontFamily: "system-ui, sans-serif" }}>
    <h1>Sign in to Aurex</h1>
    <form onSubmit={submit} noValidate>
      <label style={{ display: "block", margin: "16px 0" }}>Email<input style={{ display: "block", width: "100%", padding: 10 }} type="email" value={email} onChange={event => setEmail(event.target.value)} autoComplete="email" /></label>
      <label style={{ display: "block", margin: "16px 0" }}>Password<input style={{ display: "block", width: "100%", padding: 10 }} type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" /></label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={loading}>{loading ? "Signing in…" : "Sign in"}</button>
    </form>
    <p>Need an account? <Link href="/register">Create one</Link></p>
  </main>;
}
