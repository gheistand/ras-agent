/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        navy:  { DEFAULT: "#1A2E4A", light: "#2D5A8E" },
        teal:  { DEFAULT: "#008C8C", light: "#E6F4F4" },
        amber: { DEFAULT: "#F59E0B" },
      },
    },
  },
  plugins: [],
}

