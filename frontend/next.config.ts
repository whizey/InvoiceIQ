import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Lean production image for Docker — see frontend/Dockerfile.
  output: "standalone",
};

export default nextConfig;
