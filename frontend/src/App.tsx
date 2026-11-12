import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { LoadingState } from "./design/feedback";
import { AppShell } from "./design/layout";
import { LoginPage, RegisterPage } from "./pages/AuthPages";
import { OverviewPage, ProjectsPage } from "./pages/ProjectsPages";
import { DeliveryPage } from "./pages/DeliveryPage";
import { PaymentsPage } from "./pages/PaymentsPage";
import { PayeesPage, ScreeningPage } from "./pages/PayeesPages";
import { ReconciliationPage } from "./pages/ReconciliationPage";
import { DiagnosisPage, HealthPage, LogIntelligencePage } from "./pages/InvestigationsPages";
import { RecoveryPage } from "./pages/RecoveryPage";
import { AuditPage, ReportsPage } from "./pages/RecordsPages";
import { BuyerPortal } from "./pages/BuyerPortal";
import { Landing } from "./pages/Landing";
import { ScopePage } from "./pages/ScopePage";

function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <LoadingState label="Checking your session…" />;
  if (status === "anonymous") return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route path="/buyer" element={<BuyerPortal />} />
      <Route element={<RequireAuth />}>
        <Route path="/app" element={<AppShell />}>
          <Route index element={<OverviewPage />} />
          <Route path="projects" element={<ProjectsPage />} />
        </Route>
        <Route path="/app/p/:pid" element={<AppShell />}>
          <Route index element={<Navigate to="scope" replace />} />
          <Route path="scope" element={<ScopePage />} />
          <Route path="delivery" element={<DeliveryPage />} />
          <Route path="payments" element={<PaymentsPage />} />
          <Route path="payees" element={<PayeesPage />} />
          <Route path="screening" element={<ScreeningPage />} />
          <Route path="reconciliation" element={<ReconciliationPage />} />
          <Route path="diagnosis" element={<DiagnosisPage />} />
          <Route path="log-intelligence" element={<LogIntelligencePage />} />
          <Route path="health" element={<HealthPage />} />
          <Route path="recovery" element={<RecoveryPage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route path="audit" element={<AuditPage />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/app" replace />} />
    </Routes>
  );
}
