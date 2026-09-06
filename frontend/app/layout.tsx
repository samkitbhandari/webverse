import type { Metadata } from "next";
import "./globals.css";
import { Shell } from "@/components/Shell";

export const metadata: Metadata = {
  title: "NEXUS Ω — Knowledge Intelligence",
  description:
    "Autonomous semantic knowledge intelligence engine: a living, temporal, "
    + "explainable knowledge graph that computes what changes when information changes.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
