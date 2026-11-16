import type { CapacitorConfig } from "@capacitor/cli";

// The iOS app is the same dashboard as the website, packaged as a native app.
// `webDir` is the static export produced by `npm run build`.
const config: CapacitorConfig = {
  appId: "app.clipper.mobile",
  appName: "Clipper",
  //It means: “Put the website generated in out inside the mobile application.”
  webDir: "out",
  ios: {
    contentInset: "never", // the page handles the safe areas itself (viewport-fit=cover)
    backgroundColor: "#F5F7FB",
  },
  server: {
    iosScheme: "https", // the app's pages are served from https://localhost, which keeps cookies working
  },
};

export default config;
