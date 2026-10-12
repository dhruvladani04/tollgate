import type { Metadata } from "next";

import { ApprovalsView } from "@/components/approvals";

export const metadata: Metadata = { title: "Waiting for you" };

export default function Page() {
  return <ApprovalsView />;
}
