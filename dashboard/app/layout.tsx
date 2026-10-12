import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Shell } from "@/components/shell";

import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: { default: "Tollgate", template: "%s · Tollgate" },
  description: "What Tollgate decided about each tool call, and the calls it is holding.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full font-sans">
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
