/** @type {import('next').NextConfig} */
const nextConfig = {
  // isomorphic-dompurify requires jsdom on the server, which has native
  // dependencies that webpack cannot bundle. Externalising the package
  // tells Next.js to resolve it at runtime from node_modules instead.
  // This fixes HTTP 500 errors on pages whose client components call
  // sanitizeRulingHtml / sanitizeExcerptHtml during SSR.
  // (Next 15 promoted this from experimental.serverComponentsExternalPackages.)
  serverExternalPackages: ['isomorphic-dompurify', 'jsdom'],
};

module.exports = nextConfig;
