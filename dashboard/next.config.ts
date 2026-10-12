import type { NextConfig } from "next";

// In development the pages run on Next's own server and reach the Python API
// through a rewrite. A production build is a static export, which
// `tollgate-dashboard` serves from the same address as the API.
const development = process.env.NODE_ENV === "development";
const api = process.env.TOLLGATE_API ?? "http://127.0.0.1:8787";

const nextConfig: NextConfig = {
  ...(development
    ? {
        async rewrites() {
          return [{ source: "/api/:path*", destination: `${api}/api/:path*` }];
        },
      }
    : { output: "export" }),
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
