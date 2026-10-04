// `npm run lint` had no configuration, so it failed before checking anything. This is the
// smallest one that uses only the devDependencies already pinned in package.json. The hooks rules
// matter most here: every screen polls, and a stale closure in a polling effect is silent.
module.exports = {
  root: true,
  parser: "@typescript-eslint/parser",
  parserOptions: { ecmaVersion: 2022, sourceType: "module", ecmaFeatures: { jsx: true } },
  plugins: ["@typescript-eslint", "react-hooks"],
  extends: [
    "eslint:recommended",
    "plugin:@typescript-eslint/recommended",
    "plugin:react-hooks/recommended",
  ],
  env: { browser: true, es2022: true },
  ignorePatterns: ["dist", "node_modules"],
  rules: {
    "no-console": "warn",
  },
};
