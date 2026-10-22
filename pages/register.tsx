import { FormEvent, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/router";

export default function Register() {
  const router = useRouter();
  const [email, setEmail] = useState(""); const [password, setPassword] = useState(""); const [confirm, setConfirm] = useState("");
  const [error, setError] = useState(""); const [loading, setLoading] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setError("");
    if (!email.trim() || password.length < 12) { setError("Use a valid email and a password of at least 12 characters."); return; }
    if (password !== confirm) { setError("Passwords do not match."); return; }
    setLoading(true);
    try {
      const response = await fetch("/api/auth/register", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password, confirm_password: confirm }) });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Unable to create the account.");
      await router.replace("/login");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to create the account."); }
    finally { setLoading(false); }
  }
  return <main style={{ maxWidth: 420, margin: "12vh auto", padding: 24, fontFamily: "system-ui, sans-serif" }}>
    <h1>Create your Aurex account</h1>
    <form onSubmit={submit} noValidate>
      <label style={{ display: "block", margin: "16px 0" }}>Email<input style={{ display: "block", width: "100%", padding: 10 }} type="email" value={email} onChange={event => setEmail(event.target.value)} autoComplete="email" /></label>
      <label style={{ display: "block", margin: "16px 0" }}>Password<input style={{ display: "block", width: "100%", padding: 10 }} type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="new-password" /></label>
      <label style={{ display: "block", margin: "16px 0" }}>Confirm password<input style={{ display: "block", width: "100%", padding: 10 }} type="password" value={confirm} onChange={event => setConfirm(event.target.value)} autoComplete="new-password" /></label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={loading}>{loading ? "Creating account…" : "Create account"}</button>
    </form>
    <p>Already registered? <Link href="/login">Sign in</Link></p>
  </main>;
}
