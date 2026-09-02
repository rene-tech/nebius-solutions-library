import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { ModelDetailPage } from "../pages/ModelDetailPage";
import { ModelsPage } from "../pages/ModelsPage";
import { OperationDetailPage } from "../pages/OperationDetailPage";
import { OperationsPage } from "../pages/OperationsPage";
import { OverviewPage } from "../pages/OverviewPage";
import { SessionBoundary } from "../auth/SessionContext";
import { AccessPage } from "../pages/access/AccessPage";
import { AuditPage } from "../pages/audit/AuditPage";
import { CapacityPage } from "../pages/capacity/CapacityPage";
import { ObservabilityPage } from "../pages/observability/ObservabilityPage";
import { ConfigurationPage } from "../pages/configuration/ConfigurationPage";
import { ModelDeploymentsPage } from "../pages/modelDeployments/ModelDeploymentsPage";
import { ModelDeploymentWorkspacePage } from "../pages/modelDeployments/ModelDeploymentWorkspacePage";

export function App() {
  return (
    <SessionBoundary>
      <Routes>
        <Route path="/admin" element={<AppShell />}>
          <Route index element={<OverviewPage />} />
          <Route path="models" element={<ModelsPage />} />
          <Route path="models/:modelId" element={<ModelDetailPage />} />
          <Route path="model-deployments" element={<ModelDeploymentsPage />} />
          <Route path="model-deployments/new" element={<ModelDeploymentWorkspacePage create />} />
          <Route path="model-deployments/:deploymentName" element={<ModelDeploymentWorkspacePage />} />
          <Route path="operations" element={<OperationsPage />} />
          <Route path="operations/:operationId" element={<OperationDetailPage />} />
          <Route path="access" element={<AccessPage />} />
          <Route path="capacity" element={<CapacityPage />} />
          <Route path="observability" element={<ObservabilityPage />} />
          <Route path="configuration" element={<ConfigurationPage />} />
          <Route path="audit" element={<AuditPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/admin" replace />} />
      </Routes>
    </SessionBoundary>
  );
}
