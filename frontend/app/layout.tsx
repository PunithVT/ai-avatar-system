import './globals.css'
import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import { Toaster } from 'react-hot-toast'
import { QueryProvider } from '@/components/providers/QueryProvider'
import { ErrorBoundary } from '@/components/ui/ErrorBoundary'

const inter = Inter({ subsets: ['latin'] })

export const metadata: Metadata = {
  title: 'AI Avatar System',
  description: 'Real-time AI avatar conversation system with lip-sync animation',
  keywords: ['AI', 'avatar', 'conversation', 'lip-sync', 'real-time'],
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={inter.className}>
        <QueryProvider>
          <ErrorBoundary>
            {children}
          </ErrorBoundary>
          <Toaster
            position="top-right"
            toastOptions={{
              className: 'dark:bg-gray-800 dark:text-gray-100',
            }}
          />
        </QueryProvider>
      </body>
    </html>
  )
}
