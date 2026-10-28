import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import ProjectsPage from "./pages/ProjectsPage";
import DashboardPage from "./pages/DashboardPage";
import PlanPage from "./pages/PlanPage";
import BatchPage from "./pages/BatchPage";
import IssuesPage from "./pages/IssuesPage";
import IssueDetailPage from "./pages/IssueDetailPage";
import DependenciesPage from "./pages/DependenciesPage";
import SettingsPage from "./pages/SettingsPage";
import { StatusProvider } from "./status";

export default function App() {
  return (
    <StatusProvider>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<ProjectsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/projects/:projectKey/plan" element={<PlanPage />} />
            <Route path="/projects/:projectKey/issues" element={<IssuesPage />} />
            <Route path="/projects/:projectKey/dependencies" element={<DependenciesPage />} />
            <Route path="/batches/:batchId" element={<BatchPage />} />
            <Route path="/issues/:issueId" element={<IssueDetailPage />} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </StatusProvider>
  );
}
