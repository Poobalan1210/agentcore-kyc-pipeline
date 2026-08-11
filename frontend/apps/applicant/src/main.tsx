import React from "react";
import ReactDOM from "react-dom/client";
import "@shared/theme.css";
import "@aws-amplify/ui-react/styles.css";
import { configureAmplify } from "./amplify-config";
import { App } from "./App";

configureAmplify();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
