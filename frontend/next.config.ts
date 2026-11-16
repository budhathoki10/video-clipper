import type { NextConfig } from "next";

// Static export: the Python API serves the built dashboard from frontend/out,
// so the whole app runs at one address with no Node server in production.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
