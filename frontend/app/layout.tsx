import './globals.css'
import type { Metadata } from 'next'
import type { ReactNode } from 'react'
import { Inter, Urbanist, JetBrains_Mono } from 'next/font/google'
import { Toaster } from 'react-hot-toast'
import { QueryProvider } from '@/components/providers/QueryProvider'
import { ErrorBoundary } from '@/components/ui/ErrorBoundary'

// Exposed as CSS variables so Tailwind's fontFamily tokens resolve to them.
// display=swap keeps text visible while the face loads rather than blanking it.
const inter = Inter({
  subsets: ['latin'],
  variable: '--font-inter',
  display: 'swap',
})
const urbanist = Urbanist({
  subsets: ['latin'],
  weight: ['300', '400', '500', '600', '700'],
  variable: '--font-urbanist',
  display: 'swap',
})
const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500'],
  variable: '--font-jetbrains',
  display: 'swap',
})
// Playfair (rule 6, the italic accent) is deliberately NOT loaded: nothing in
// the app uses `.accent-italic` yet, and a webfont nobody renders is a wasted
// download on every first visit. The class falls back to Georgia; add the
// next/font import back alongside its first real use.

const FONT_VARS = [inter, urbanist, jetbrainsMono].map((f) => f.variable).join(' ')

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'http://localhost:3000'
const SITE_TITLE = 'AvatarAI — Real-Time AI Avatar with Lip-Sync & Voice Cloning'
const SITE_DESCRIPTION =
  'Open-source AI talking avatar platform: upload a photo, clone a voice, and have ' +
  'real-time conversations with photorealistic lip-sync video. Powered by Claude, ' +
  'Whisper, Chatterbox TTS, and MuseTalk. Self-host everything.'

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: SITE_TITLE,
    template: '%s · AvatarAI',
  },
  description: SITE_DESCRIPTION,
  applicationName: 'AvatarAI',
  keywords: [
    'AI avatar', 'talking avatar', 'talking head', 'digital human', 'lip sync',
    'voice cloning', 'text to speech', 'speech to text', 'real-time avatar',
    'AI companion', 'Claude', 'Whisper', 'MuseTalk', 'Chatterbox TTS',
    'open source avatar', 'self-hosted AI',
  ],
  alternates: { canonical: '/' },
  openGraph: {
    type: 'website',
    url: SITE_URL,
    siteName: 'AvatarAI',
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    locale: 'en_US',
  },
  twitter: {
    card: 'summary',
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
  },
  robots: {
    index: true,
    follow: true,
    googleBot: { index: true, follow: true, 'max-image-preview': 'large' },
  },
}

// Structured data (schema.org SoftwareApplication) — lets search engines show
// a rich result card instead of a bare blue link.
const JSON_LD = {
  '@context': 'https://schema.org',
  '@type': 'SoftwareApplication',
  name: 'AvatarAI',
  applicationCategory: 'MultimediaApplication',
  operatingSystem: 'Web, Linux, Docker',
  description: SITE_DESCRIPTION,
  url: SITE_URL,
  offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
  softwareHelp: 'https://github.com/PunithVT/ai-avatar-system',
  sameAs: ['https://github.com/PunithVT/ai-avatar-system'],
  featureList: [
    'Real-time lip-sync avatar video (MuseTalk)',
    'Zero-shot voice cloning in 23 languages (Chatterbox)',
    'Streaming LLM conversations (Claude / GPT)',
    'Whisper speech-to-text',
    'Self-hostable with Docker',
  ],
}

export default function RootLayout({
  children,
}: {
  children: ReactNode
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(JSON_LD) }}
        />
      </head>
      <body className={`${FONT_VARS} font-sans antialiased`}>
        {/* Skip link — appears only on keyboard focus, lets users bypass the nav */}
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-[100]
 focus:px-4 focus:py-2 focus:rounded-lg focus:bg-primary-600 focus:text-white
 focus:shadow-sm focus:outline-none focus:ring-2 focus:ring-primary-300"
        >
          Skip to main content
        </a>
        <QueryProvider>
          <ErrorBoundary>
            <div id="main-content">{children}</div>
          </ErrorBoundary>
          <Toaster
            position="top-right"
            toastOptions={{
              // A card, like every other floating surface in the system.
              className: 'border border-gray-200 !rounded-lg !bg-white !text-sm !text-gray-900 !shadow-lg',
              duration: 4000,
            }}
          />
        </QueryProvider>
      </body>
    </html>
  )
}
