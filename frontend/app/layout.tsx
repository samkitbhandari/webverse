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
    // Browser extensions (Grammarly, password managers, dark-mode add-ons)
    // inject attributes such as `data-gr-ext-installed` onto <html> and <body>
    // before React hydrates, which React reports as a hydration mismatch and
    // then recovers from by re-rendering the entire tree on the client.
    //
    // suppressHydrationWarning applies to the element it is on and one level
    // deep only, so this silences the extension noise on these two wrappers
    // without hiding a genuine mismatch anywhere inside the app.
    <html lang="en" suppressHydrationWarning>
      <body suppressHydrationWarning>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
