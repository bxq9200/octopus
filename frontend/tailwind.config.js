/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        mono: ['"Maple Mono CN"', '"Maple Mono"', '"SF Mono"', '"Fira Code"', 'Cascadia Code', 'monospace'],
      },
      colors: {
        octopus: {
          bg: '#08080c',
          panel: '#101018',
          border: '#22222e',
          primary: '#22d3ee',
          accent: '#a855f7',
          text: '#e8e8f2',
          muted: '#7a7a8c',
          success: '#34d399',
          warning: '#f59e0b',
          error: '#fb7185',
          chat: '#a5e6e3',
          menu: '#c8e2c6',
          brand: '#a4dcf9',
          // 节点状态色（深色冷电感）
          idle: '#64748b',
          running: '#22d3ee',
          ready: '#818cf8',
          done: '#34d399',
          danger: '#fb7185',
        }
      }
    },
  },
  plugins: [],
}
