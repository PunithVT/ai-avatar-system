/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  images: {
    remotePatterns: [
      {
        protocol: 'http',
        hostname: 'localhost',
      },
      {
        protocol: 'https',
        hostname: '*.s3.*.amazonaws.com',
      },
      {
        protocol: 'https',
        hostname: 's3.amazonaws.com',
      },
      ...(process.env.NEXT_PUBLIC_S3_BUCKET_DOMAIN
        ? [{
            protocol: 'https',
            hostname: process.env.NEXT_PUBLIC_S3_BUCKET_DOMAIN,
          }]
        : []),
      ...(process.env.NEXT_PUBLIC_CLOUDFRONT_DOMAIN
        ? [{
            protocol: 'https',
            hostname: process.env.NEXT_PUBLIC_CLOUDFRONT_DOMAIN,
          }]
        : []),
    ],
  },
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000',
    NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000',
  },
};

module.exports = nextConfig;
