import React from "react";
import ReactDOM from "react-dom/client";
// Amplify's stylesheet first, so the console's tokens and overrides win.
import "@aws-amplify/ui-react/styles.css";
import "@shared/theme.css";
import "./amplify-theme.css";
import { configureAmplify } from "./amplify-config";
import { App } from "./App";

configureAmplify();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
