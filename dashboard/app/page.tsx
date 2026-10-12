import type { Metadata } from "next";

import { DecisionsView } from "@/components/decisions";

export const metadata: Metadata = { title: "Decisions" };

export default function Page() {
  return <DecisionsView />;
}
