import { useState } from "react";
import { Button } from "@shared/components/Button";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { IntakeSelection } from "./types";

const JURISDICTIONS = [
  { code: "us", label: "United States" },
  { code: "uk", label: "United Kingdom" },
  { code: "au", label: "Australia" },
  { code: "jp", label: "Japan" },
];

const PRODUCTS = [
  { code: "current_account", label: "Current account" },
  { code: "savings_account", label: "Savings account" },
];

export function IntakeStep({ onContinue }: { onContinue: (selection: IntakeSelection) => void }) {
  const [jurisdiction, setJurisdiction] = useState(JURISDICTIONS[0].code);
  const [productApplied, setProductApplied] = useState(PRODUCTS[0].code);

  return (
    <Card>
      <CardHeader>
        <h2>Start your application</h2>
      </CardHeader>
      <CardBody>
        <div className="kyc-form-row">
          <label htmlFor="jurisdiction">Country</label>
          <select
            id="jurisdiction"
            value={jurisdiction}
            onChange={(e) => setJurisdiction(e.target.value)}
          >
            {JURISDICTIONS.map((j) => (
              <option key={j.code} value={j.code}>
                {j.label}
              </option>
            ))}
          </select>
        </div>
        <div className="kyc-form-row">
          <label htmlFor="product">Product</label>
          <select
            id="product"
            value={productApplied}
            onChange={(e) => setProductApplied(e.target.value)}
          >
            {PRODUCTS.map((p) => (
              <option key={p.code} value={p.code}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
        <p className="kyc-hint">
          You will be asked to complete a short liveness check and upload one identity document.
          This normally takes under two minutes.
        </p>
        <Button variant="primary" onClick={() => onContinue({ jurisdiction, productApplied })}>
          Continue
        </Button>
      </CardBody>
    </Card>
  );
}
