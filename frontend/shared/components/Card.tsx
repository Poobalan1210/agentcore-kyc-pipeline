import { HTMLAttributes } from "react";
import "./Card.css";

export function Card({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={["kyc-card", className].filter(Boolean).join(" ")} {...rest}>
      {children}
    </div>
  );
}

export function CardHeader({ children }: { children: React.ReactNode }) {
  return <div className="kyc-card__header">{children}</div>;
}

export function CardBody({ children }: { children: React.ReactNode }) {
  return <div className="kyc-card__body">{children}</div>;
}
