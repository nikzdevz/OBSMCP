import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f5f7ff',
          100: '#e7ecff',
          500: '#5b71ff',
          600: '#4256f0',
          700: '#3442c8',
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
