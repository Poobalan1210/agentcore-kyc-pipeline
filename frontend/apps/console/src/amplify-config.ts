/**
 * Amplify Auth configuration (task 14.5, F-42).
 *
 * The user pool is the console's one sign-in; the API's JWT authorizer validates the same tokens.
 * The identity pool is there only so `FaceLivenessDetector` can sign its direct request to the
 * Rekognition streaming service (R33.7). Amplify will not render the detector without a
 * configured Auth backend (F-42).
 *
 * All values come from `KycAuth`'s CDK outputs. There is no fallback: a missing variable fails at
 * start-up rather than pointing the console at nothing.
 */

import { Amplify } from "aws-amplify";

function required(name: string): string {
  const value = import.meta.env[name];
  if (!value) {
    throw new Error(`${name} is not set — see frontend/apps/console/.env.local.example`);
  }
  return value as string;
}

export function configureAmplify(): void {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: required("VITE_USER_POOL_ID"),
        userPoolClientId: required("VITE_USER_POOL_CLIENT_ID"),
        identityPoolId: required("VITE_IDENTITY_POOL_ID"),
        // The pool's username is the email address, so the sign-in form asks for one.
        loginWith: { email: true },
      },
    },
  });
}
