import type { Metadata } from "next";

import { ThresholdsView } from "@/components/thresholds";

export const metadata: Metadata = { title: "Thresholds" };

export default function Page() {
  return <ThresholdsView />;
}
