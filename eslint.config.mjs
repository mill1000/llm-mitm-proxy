import globals from "globals";

export default [
  {
    ignores: ["node_modules/"],
  },
  {
    files: ["ui/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
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
