/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  images: {
    domains: [
      'localhost',
      's3.amazonaws.com',
      // Add your S3 bucket domain
      process.env.NEXT_PUBLIC_S3_BUCKET_DOMAIN,
      // Add CloudFront domain if using
      process.env.NEXT_PUBLIC_CLOUDFRONT_DOMAIN,
    ].filter(Boolean),
  },
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000',
    NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000',
  },
};

module.exports = nextConfig;
