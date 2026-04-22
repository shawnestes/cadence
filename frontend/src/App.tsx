import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { Campaigns } from "@/pages/Campaigns";
import { CampaignDetail } from "@/pages/CampaignDetail";
import { Dashboard } from "@/pages/Dashboard";
import { Recipients } from "@/pages/Recipients";
import { RecipientDetail } from "@/pages/RecipientDetail";
import { Suppressions } from "@/pages/Suppressions";
import { Workflows } from "@/pages/Workflows";

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/recipients" element={<Recipients />} />
        <Route path="/recipients/:id" element={<RecipientDetail />} />
        <Route path="/suppressions" element={<Suppressions />} />
        <Route path="/campaigns" element={<Campaigns />} />
        <Route path="/campaigns/:id" element={<CampaignDetail />} />
        <Route path="/workflows" element={<Workflows />} />
      </Route>
    </Routes>
  );
}
