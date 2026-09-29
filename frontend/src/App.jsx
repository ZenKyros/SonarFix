import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import ProjectsPage from "./pages/ProjectsPage";
import PlanPage from "./pages/PlanPage";
import BatchPage from "./pages/BatchPage";
import IssuesPage from "./pages/IssuesPage";
import IssueDetailPage from "./pages/IssueDetailPage";

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<ProjectsPage />} />
          <Route path="/projects/:projectKey/plan" element={<PlanPage />} />
          <Route path="/projects/:projectKey/issues" element={<IssuesPage />} />
          <Route path="/batches/:batchId" element={<BatchPage />} />
          <Route path="/issues/:issueId" element={<IssueDetailPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}
