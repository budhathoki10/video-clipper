import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Shell } from "@/components/shell";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: { default: "Clipper", template: "%s · Clipper" },
  description: "Turn long videos and live streams into captioned clips ready to post.",
  applicationName: "Clipper",
  manifest: "/manifest.webmanifest",
  // iPhone: opens full screen from the home screen, with its own name and icon.
  appleWebApp: { capable: true, title: "Clipper", statusBarStyle: "default" },
  icons: {
    icon: [{ url: "/icon-192.png", sizes: "192x192", type: "image/png" }],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
  },
  formatDetection: { telephone: false },
  other: { "apple-mobile-web-app-capable": "yes" },
};

// viewportFit "cover" lets the layout use the safe-area insets (home indicator, notch).
export const viewport: Viewport = { themeColor: "#f5f7fb", width: "device-width", initialScale: 1, viewportFit: "cover" };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
