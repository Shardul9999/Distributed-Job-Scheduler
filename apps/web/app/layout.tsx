import type { Metadata } from "next";
import { Archivo, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";
import { themeInitScript } from "@/lib/theme";

// The same pairing Codity uses across their marketing site and product
// dashboard: Archivo for UI, JetBrains Mono for anything the machine wrote --
// ids, cron expressions, payloads, stack traces. Self-hosted by next/font, so
// there is no external request at runtime and no layout shift on first paint.
const archivo = Archivo({
  subsets: ["latin"],
  variable: "--font-archivo",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Codity — Job Scheduler",
  description: "Distributed job scheduler dashboard",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // suppressHydrationWarning: the inline script below mutates `data-theme`
    // before React hydrates, so the server's markup and the client's DOM
    // legitimately differ on that one attribute.
    <html
      lang="en"
      className={`${archivo.variable} ${jetbrainsMono.variable}`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="font-sans antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
