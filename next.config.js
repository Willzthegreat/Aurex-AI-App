/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${process.env.AUREX_API_ORIGIN || "http://127.0.0.1:8001"}/api/:path*` }];
  },
};

module.exports = nextConfig;
