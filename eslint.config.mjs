import globals from "globals";

export default [
  {
    ignores: ["node_modules/", "llm_proxy/web/"],
  },
  {
    files: ["ui/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.es2021,
      },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": "error",
      "no-empty": ["error", { allowEmptyCatch: true }],
      eqeqeq: ["error", "smart"],
    },
  },
];
