/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    // Proxy API + WebSocket to the FastAPI backend so the browser sees one
    // origin and CORS never enters the picture during a live demo.
    const backend = process.env.NEXUS_API ?? "http://127.0.0.1:8000";
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
      { source: "/ws", destination: `${backend}/ws` },
    ];
  },
};
export default nextConfig;
