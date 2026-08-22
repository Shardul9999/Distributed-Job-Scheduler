/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The dashboard talks to the API directly from the browser over CORS
  // (the API already allow-lists http://localhost:3000), so no rewrites are
  // needed. The base URL is injected via NEXT_PUBLIC_API_BASE.
};

export default nextConfig;
