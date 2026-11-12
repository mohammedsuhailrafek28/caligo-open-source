import { useState, type FormEvent } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { errorMessage, Notice } from "../design/feedback";
import { Button, InputField } from "../design/ui";

function AuthFrame({ title, intro, children, footer }: { title: string; intro: string; children: React.ReactNode; footer: React.ReactNode }) {
  return (
    <main className="mx-auto max-w-md px-6 py-16">
      <Link to="/" className="font-display text-2xl">Caligo</Link>
      <h1 className="mt-10 font-display text-4xl">{title}</h1>
      <p className="mt-3 text-ink-soft">{intro}</p>
      <div className="mt-8">{children}</div>
      <p className="mt-8 text-sm text-ink-soft">{footer}</p>
    </main>
  );
}

function useAuthForm(action: () => Promise<void>) {
  const navigate = useNavigate();
  const location = useLocation();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const from = (location.state as { from?: string } | null)?.from ?? "/app";
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await action();
      navigate(from, { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return { submit, error, busy };
}

export function LoginPage() {
  const { login, status, expired } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const form = useAuthForm(() => login(email, password));
  if (status === "authenticated") return <Navigate to="/app" replace />;
  return (
    <AuthFrame title="Sign in" intro="Your session lasts eight hours. Caligo never asks for payment credentials here." footer={<>No account? <Link className="text-terracotta-deep underline" to="/register">Create one</Link>.</>}>
      <form onSubmit={form.submit} className="space-y-5">
        {expired && <Notice tone="info">Your session ended. Sign in again to continue.</Notice>}
        <InputField label="Email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        <InputField label="Password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        {form.error && <Notice tone="error">{form.error}</Notice>}
        <Button type="submit" busy={form.busy}>Sign in</Button>
      </form>
    </AuthFrame>
  );
}

export function RegisterPage() {
  const { register, status } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const form = useAuthForm(() => register(name, email, password));
  if (status === "authenticated") return <Navigate to="/app" replace />;
  return (
    <AuthFrame title="Create an account" intro="A seller account owns its projects. Other sellers can never see them." footer={<>Already registered? <Link className="text-terracotta-deep underline" to="/login">Sign in</Link>.</>}>
      <form onSubmit={form.submit} className="space-y-5">
        <InputField label="Display name" autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} />
        <InputField label="Email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        <InputField label="Password" type="password" autoComplete="new-password" minLength={12} required hint="At least 12 characters." value={password} onChange={(e) => setPassword(e.target.value)} />
        {form.error && <Notice tone="error">{form.error}</Notice>}
        <Button type="submit" busy={form.busy}>Create account</Button>
      </form>
    </AuthFrame>
  );
}
