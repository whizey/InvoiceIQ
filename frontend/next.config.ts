import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Lean production image for Docker — see frontend/Dockerfile.
  output: "standalone",
  turbopack: {
    root: path.resolve(__dirname, ".."),
  },
};

export default nextConfig;
