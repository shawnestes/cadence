import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { Dashboard } from "@/pages/Dashboard";
import { Recipients } from "@/pages/Recipients";
import { Suppressions } from "@/pages/Suppressions";
import { Workflows } from "@/pages/Workflows";

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/recipients" element={<Recipients />} />
        <Route path="/suppressions" element={<Suppressions />} />
        <Route path="/workflows" element={<Workflows />} />
      </Route>
    </Routes>
  );
}
