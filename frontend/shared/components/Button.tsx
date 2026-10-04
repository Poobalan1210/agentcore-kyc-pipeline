import { ButtonHTMLAttributes, forwardRef } from "react";
import "./Button.css";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  loading?: boolean;
}

/**
 * The one button component the console uses. Deliberately has no size prop beyond the default:
 * a proliferation of button sizes is how a design system drifts into inconsistency.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = "secondary", loading = false, disabled, children, className, ...rest }, ref) => {
    return (
      <button
        ref={ref}
        className={["kyc-button", `kyc-button--${variant}`, className].filter(Boolean).join(" ")}
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        {...rest}
      >
        {loading ? <span className="kyc-button__spinner" aria-hidden="true" /> : null}
        <span>{children}</span>
      </button>
    );
  },
);
Button.displayName = "Button";
