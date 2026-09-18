/** @type {import('tailwindcss').Config} */
//
// HireAI design system. The rules it states are load-bearing, not decoration:
//
//   1. Monochrome canvas — #0a0a0a ink on #fcfcfd. primary-600 is reserved for
//      buttons, links and ONE accent per page. Never paint a surface with it.
//   2. Urbanist headlines, Inter body, tracking -0.015em on headings.
//   3. JetBrains Mono for 11px uppercase eyebrow labels — the signature.
//   4. No gradients. Ever. Reach for a darker swatch instead.
//   5. Rounded surfaces — 16px cards, 20-24px hero, 8px controls.
//   6. One italic Playfair accent, sparingly.
//
// There is deliberately no gradient utility and no glow shadow defined below,
// so rule 4 is enforced by the config rather than by remembering it.
module.exports = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './store/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        // Ink: the near-black used for hero panels and highest-contrast text.
        // Distinct from gray-900, which is the body text colour.
        ink: '#0a0a0a',
        gray: {
          25: '#fcfcfd', // page canvas
          50: '#f9fafb',
          100: '#f2f4f7',
          200: '#eaecf0',
          300: '#d0d5dd',
          400: '#98a2b3',
          500: '#667085',
          600: '#475467',
          700: '#344054',
          800: '#1d2939',
          900: '#101828',
        },
        primary: {
          50: '#f0f5ff',
          100: '#e0eaff',
          200: '#c7d7fe',
          300: '#a4bcfd',
          400: '#8098f9',
          500: '#6172f3',
          600: '#444ce7', // the brand accent
          700: '#3538cd',
          800: '#2d31a6',
          900: '#2b2f83',
        },
        success: { 50: '#ecfdf3', 500: '#12b76a', 600: '#039855', 700: '#027a48' },
        warning: { 50: '#fffaeb', 500: '#f79009', 600: '#dc6803', 700: '#b54708' },
        error: { 50: '#fef3f2', 400: '#f97066', 500: '#f04438', 600: '#d92d20', 700: '#b42318' },
      },

      fontFamily: {
        // var(--font-*) are injected by next/font in app/layout.tsx; the
        // literal names are the fallback if a face fails to load.
        sans: ['var(--font-inter)', 'Inter', 'system-ui', '-apple-system', 'sans-serif'],
        display: ['var(--font-urbanist)', 'Urbanist', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['var(--font-jetbrains)', '"JetBrains Mono"', 'ui-monospace', 'monospace'],
        // Rule 6: the single italic accent.
        accent: ['var(--font-playfair)', '"Playfair Display"', 'Georgia', 'serif'],
      },

      fontSize: {
        // Body is 15px, not Tailwind's 16px default — the system is set
        // slightly tighter than stock and it shows across dense UI.
        base: ['15px', { lineHeight: '1.55' }],
        eyebrow: ['11px', { lineHeight: '1.5', letterSpacing: '0.15em', fontWeight: '500' }],
        'display-sm': ['1.875rem', { lineHeight: '1.3', letterSpacing: '-0.01em', fontWeight: '600' }],
        'display-md': ['2.25rem', { lineHeight: '1.2', letterSpacing: '-0.02em', fontWeight: '700' }],
        'display-lg': ['3rem', { lineHeight: '1.2', letterSpacing: '-0.02em', fontWeight: '700' }],
      },

      borderRadius: {
        xs: '4px',
        sm: '6px',
        md: '8px', // buttons and inputs
        lg: '12px',
        xl: '16px', // standard card
        '2xl': '20px', // hero / featured panel
      },

      boxShadow: {
        // Subtle by default. Nothing heavier than sm in ordinary UI; lg is for
        // modals and floating menus only.
        xs: '0 1px 2px 0 rgba(16,24,40,.05)',
        sm: '0 1px 3px 0 rgba(16,24,40,.1), 0 1px 2px -1px rgba(16,24,40,.1)',
        md: '0 4px 8px -2px rgba(16,24,40,.1), 0 2px 4px -2px rgba(16,24,40,.06)',
        lg: '0 12px 16px -4px rgba(16,24,40,.08), 0 4px 6px -2px rgba(16,24,40,.03)',
        xl: '0 20px 24px -4px rgba(16,24,40,.08), 0 8px 8px -4px rgba(16,24,40,.03)',
        ring: '0 0 0 4px rgba(68,76,231,.12)',
      },

      letterSpacing: {
        heading: '-0.015em',
        eyebrow: '0.15em',
      },

      animation: {
        'fade-up': 'fadeUp .4s cubic-bezier(.16,1,.3,1) both',
        'fade-in': 'fadeIn .3s ease both',
        'pulse-soft': 'pulseSoft 2s cubic-bezier(.4,0,.6,1) infinite',
      },
      keyframes: {
        // Short and eased rather than bouncy. The system is restrained; motion
        // should confirm an action, not perform.
        fadeUp: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        fadeIn: { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
        pulseSoft: { '0%,100%': { opacity: '1' }, '50%': { opacity: '.5' } },
      },
    },
  },
  plugins: [],
}
