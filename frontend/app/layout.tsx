import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Inside the Agent | Document QA Pipeline",
  description: "Follow a document from source text to a grounded answer.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
